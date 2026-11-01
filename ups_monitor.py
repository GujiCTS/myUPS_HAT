#!/usr/bin/env python3
"""透過 INA219 監控 Waveshare UPS HAT (C)，並執行低電量關機。"""

import argparse
import configparser
import logging
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).with_name("ups-monitor.ini")
DEFAULTS = dict(bus="1", address="0x43", low_percent="20", countdown_seconds="30",
                battery_only="yes", cancel_on_recovery="yes", recovery_percent="23",
                current_deadband_ma="10", poll_seconds="1", status_seconds="10",
                empty_voltage="3.0", full_voltage="4.2", low_samples="3")
LOG = logging.getLogger("ups-monitor")


def load_config(path):
    """載入並驗證設定，避免錯誤設定導致非預期關機。"""
    parser = configparser.ConfigParser()
    parser.read_dict({"monitor": DEFAULTS})
    with open(path, encoding="utf-8-sig") as stream:
        parser.read_file(stream)
    c = parser["monitor"]
    result = {}
    for key in DEFAULTS:
        if key in ("battery_only", "cancel_on_recovery"):
            result[key] = c.getboolean(key)
        elif key in ("bus", "address", "low_samples"):
            result[key] = int(c[key], 0)
        else:
            result[key] = float(c[key])
            if not math.isfinite(result[key]):
                raise ValueError(f"設定 {key} 必須是有限數值")
    if not 0 <= result["low_percent"] < result["recovery_percent"] <= 100:
        raise ValueError("門檻必須滿足 0 ≤ low_percent < recovery_percent ≤ 100")
    for key in ("countdown_seconds", "current_deadband_ma"):
        if result[key] < 0:
            raise ValueError(f"設定 {key} 不可小於零")
    for key in ("poll_seconds", "status_seconds", "low_samples"):
        if result[key] <= 0:
            raise ValueError(f"設定 {key} 必須大於零")
    if result["poll_seconds"] > 1:
        raise ValueError("poll_seconds 不可超過 1 秒，以維持倒數更新頻率")
    if not 0 < result["empty_voltage"] < result["full_voltage"] <= 5:
        raise ValueError("電壓範圍必須滿足 0 < empty_voltage < full_voltage ≤ 5")
    if result["bus"] < 0 or not 0x03 <= result["address"] <= 0x77:
        raise ValueError("I²C 匯流排或位址設定不正確")
    return result


@dataclass
class Reading:
    """單次量測值；電流正值表示充電。"""
    voltage: float
    current_ma: float
    percent: float
    state: str


class INA219:
    """使用官方範例的校正值與設定，透過大端序暫存器讀寫。"""
    def __init__(self, c):
        from smbus import SMBus
        self.c = c
        self.bus = SMBus(c["bus"])
        try:
            self.write(5, 26868)
            self.write(0, 0x0EEF)
            time.sleep(0.15)
        except Exception:
            self.bus.close()
            raise

    def write(self, register, value):
        """以大端序寫入 16 位元暫存器。"""
        self.bus.write_i2c_block_data(self.c["address"], register,
                                     [value >> 8, value & 255])

    def read(self, register):
        """讀取 16 位元暫存器。"""
        data = self.bus.read_i2c_block_data(self.c["address"], register, 2)
        return (data[0] << 8) | data[1]

    def sample(self):
        """量測電壓與電流；拒絕溢位及不合理數據。"""
        if self.read(0) != 0x0EEF or self.read(5) != 26868:
            self.write(5, 26868)
            self.write(0, 0x0EEF)
            raise OSError("INA219 設定改變，已重設，等待下一次量測")
        raw_bus = self.read(2)
        if raw_bus & 1:
            raise OSError("INA219 量測溢位")
        voltage = (raw_bus >> 3) * 0.004
        raw_current = self.read(4)
        current = (raw_current - 65536 if raw_current & 0x8000 else raw_current) * 0.1524
        if not 2.0 <= voltage <= 4.6:
            raise OSError(f"電池電壓不合理：{voltage:.3f} V")
        c = self.c
        percent = max(0, min(100, (voltage - c["empty_voltage"]) /
                             (c["full_voltage"] - c["empty_voltage"]) * 100))
        deadband = c["current_deadband_ma"]
        state = "放電" if current < -deadband else "充電" if current > deadband else "狀態不明"
        return Reading(voltage, current, percent, state)

    def close(self):
        """釋放 I²C 資源。"""
        self.bus.close()


class ShutdownPolicy:
    """以單調時間管理倒數；量測失敗時暫停並要求重新確認。"""
    def __init__(self, c):
        self.c = c
        self.deadline = None
        self.low_count = 0
        self.latched = False

    def sensor_error(self):
        """取消目前倒數，但保留不可取消模式的已觸發狀態。"""
        self.deadline = None
        self.low_count = 0
        if self.c["cancel_on_recovery"]:
            self.latched = False

    def update(self, reading, now):
        """回傳剩餘秒數；None 表示未啟動倒數。"""
        c = self.c
        recovered = reading.percent >= c["recovery_percent"] or reading.state == "充電"
        if c["cancel_on_recovery"] and recovered:
            self.deadline = None
            self.latched = False
            self.low_count = 0
            return None
        low = reading.percent <= c["low_percent"] and (
            not c["battery_only"] or reading.state == "放電")
        if self.deadline is None:
            self.low_count = self.low_count + 1 if low else 0
            if self.low_count >= c["low_samples"] or self.latched:
                self.latched = True
                self.deadline = now + c["countdown_seconds"]
        return None if self.deadline is None else max(0, math.ceil(self.deadline - now))


def notify(message):
    """記錄訊息並提示目前登入的終端機使用者。"""
    LOG.warning(message)
    try:
        subprocess.run(["/usr/bin/wall", "--nobanner"], input=message + "\n",
                       text=True, check=True, timeout=3, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("終端機廣播失敗：%s", exc)


def run(c, dry_run):
    """執行持續監控；僅在最新有效量測後提出關機要求。"""
    sensor = INA219(c)
    policy = ShutdownPolicy(c)
    last_remaining = None
    next_status = 0
    errors = 0
    try:
        while True:
            try:
                reading = sensor.sample()
            except OSError as exc:
                errors += 1
                policy.sensor_error()
                if errors == 1 or errors % 30 == 0:
                    notify(f"I²C 量測失敗，自動關機倒數已停止，將重試：{exc}")
                last_remaining = None
                time.sleep(c["poll_seconds"])
                continue
            if errors:
                LOG.warning("I²C 量測已恢復，重新確認關機條件")
                errors = 0
            now = time.monotonic()
            remaining = policy.update(reading, now)
            if now >= next_status:
                LOG.info("電量估算 %.1f%% | %.3f V | %+.1f mA | %s",
                         reading.percent, reading.voltage, reading.current_ma, reading.state)
                next_status = now + c["status_seconds"]
            if remaining is not None and remaining != last_remaining:
                LOG.warning("%s低電量，倒數 %d 秒後關機", "[測試] " if dry_run else "", remaining)
                if last_remaining is None or remaining in (10, 5, 0):
                    notify(f"{'[測試，不會關機] ' if dry_run else ''}低電量，{remaining} 秒後關機")
            elif remaining is None and last_remaining is not None:
                notify("充電或電量回升，自動關機已取消")
            last_remaining = remaining
            if remaining == 0:
                if dry_run:
                    LOG.warning("測試完成：未執行關機")
                    return 0
                try:
                    subprocess.run(["/usr/bin/systemctl", "poweroff"], check=True, timeout=15)
                except (OSError, subprocess.SubprocessError) as exc:
                    LOG.error("關機指令失敗：%s；10 秒後重新量測與嘗試", exc)
                    time.sleep(10)
                    continue
                LOG.warning("系統已接受關機要求")
                return 0
            time.sleep(c["poll_seconds"])
    finally:
        sensor.close()


def main():
    """處理命令列參數與錯誤結束狀態。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="設定檔路徑")
    parser.add_argument("--dry-run", action="store_true", help="讀取實際硬體，但不執行關機")
    parser.add_argument("--check-config", action="store_true", help="僅檢查設定檔")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        c = load_config(args.config)
        if args.check_config:
            LOG.info("設定檔檢查通過")
            return 0
        return run(c, args.dry_run)
    except KeyboardInterrupt:
        LOG.info("使用者停止監控，未提出新的關機要求")
        return 0
    except (OSError, ValueError, configparser.Error, ImportError) as exc:
        LOG.error("無法執行監控：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

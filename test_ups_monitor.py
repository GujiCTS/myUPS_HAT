"""以模擬時間與 I²C 驅動驗證政策，不能取代實機測試。"""
import sys
import types
import unittest
from unittest.mock import patch

import ups_monitor as m


class FakeBus:
    """提供可控制的暫存器與故障。"""
    def __init__(self, bus):
        self.registers = {2: (900 << 3) | 2, 4: 0xFC18}
        self.closed = False

    def write_i2c_block_data(self, address, register, data):
        self.registers[register] = data[0] << 8 | data[1]

    def read_i2c_block_data(self, address, register, length):
        value = self.registers[register]
        return [value >> 8, value & 255]

    def close(self):
        self.closed = True


class MonitorTests(unittest.TestCase):
    """驗證倒數、取消、異常及暫存器轉換。"""
    def setUp(self):
        self.c = m.load_config(m.DEFAULT_CONFIG)
        self.low = m.Reading(3.2, -200, 16.7, "放電")

    def arm(self, p):
        self.assertIsNone(p.update(self.low, 0))
        self.assertIsNone(p.update(self.low, 1))
        self.assertEqual(p.update(self.low, 2), 30)

    def test_countdown(self):
        p = m.ShutdownPolicy(self.c)
        self.arm(p)
        self.assertEqual(p.update(self.low, 31), 1)
        self.assertEqual(p.update(self.low, 32), 0)

    def test_recovery_and_charging(self):
        for r in (m.Reading(3.4, -100, 24, "放電"), m.Reading(3.2, 100, 16, "充電")):
            p = m.ShutdownPolicy(self.c)
            self.arm(p)
            self.assertIsNone(p.update(r, 32))

    def test_unknown_does_not_arm_or_cancel(self):
        r = m.Reading(3.2, 0, 16, "狀態不明")
        p = m.ShutdownPolicy(self.c)
        for n in range(5):
            self.assertIsNone(p.update(r, n))
        self.arm(p)
        self.assertEqual(p.update(r, 32), 0)

    def test_no_cancel(self):
        self.c["cancel_on_recovery"] = False
        p = m.ShutdownPolicy(self.c)
        self.arm(p)
        self.assertEqual(p.update(m.Reading(4, 100, 80, "充電"), 32), 0)

    def test_sensor_error_restarts_confirmation(self):
        p = m.ShutdownPolicy(self.c)
        self.arm(p)
        p.sensor_error()
        self.assertIsNone(p.update(self.low, 100))
        self.assertIsNone(p.update(self.low, 101))
        self.assertEqual(p.update(self.low, 102), 30)

    def test_all_power_sources(self):
        self.c.update(battery_only=False, cancel_on_recovery=False)
        p = m.ShutdownPolicy(self.c)
        r = m.Reading(3.2, 0, 16, "狀態不明")
        for n in range(3):
            result = p.update(r, n)
        self.assertEqual(result, 30)

    def test_fake_driver(self):
        with patch.dict(sys.modules, smbus=types.SimpleNamespace(SMBus=FakeBus)):
            sensor = m.INA219(self.c)
            r = sensor.sample()
            self.assertAlmostEqual(r.voltage, 3.6)
            self.assertAlmostEqual(r.percent, 50)
            self.assertAlmostEqual(r.current_ma, -152.4)
            self.assertEqual(r.state, "放電")
            sensor.bus.registers[2] |= 1
            with self.assertRaises(OSError):
                sensor.sample()
            sensor.close()
            self.assertTrue(sensor.bus.closed)

    def test_shutdown_is_suppressed_in_dry_run(self):
        self.c.update(low_samples=1, countdown_seconds=0)
        sensor = types.SimpleNamespace(sample=lambda: self.low, close=lambda: None)
        with patch.object(m, "INA219", return_value=sensor), patch.object(m, "notify"), \
                patch.object(m.subprocess, "run") as command:
            self.assertEqual(m.run(self.c, True), 0)
            command.assert_not_called()

    def test_shutdown_command(self):
        self.c.update(low_samples=1, countdown_seconds=0)
        sensor = types.SimpleNamespace(sample=lambda: self.low, close=lambda: None)
        with patch.object(m, "INA219", return_value=sensor), patch.object(m, "notify"), \
                patch.object(m.subprocess, "run") as command:
            self.assertEqual(m.run(self.c, False), 0)
            command.assert_called_once_with(["/usr/bin/systemctl", "poweroff"], check=True, timeout=15)


if __name__ == "__main__":
    unittest.main()

# Raspberry Pi Zero 2 W：UPS HAT (C) 電池監控

適用目標：Raspberry Pi OS／Debian 13 trixie、aarch64、Python 3、Waveshare UPS HAT (C)。未在該實機執行，不能宣稱硬體相容性已驗證。

## 安裝

將本目錄的 `ups_monitor.py`、`ups-monitor.ini`、`ups-monitor.service` 複製到 Raspberry Pi 的同一目錄，再於該目錄執行：

```bash
sudo apt update
sudo apt install python3 python3-smbus i2c-tools
sudo raspi-config
```

在介面設定中啟用 I2C，重新開機。若系統沒有 raspi-config，先確認使用的系統映像及其 I²C 啟用方式；不要直接套用其他發行版的開機設定。

```bash
sudo reboot
# 重新登入後，在程式檔案目錄執行：
ls -l /dev/i2c-1
sudo i2cdetect -y 1
python3 -c 'import smbus; print("smbus 匯入成功")'
python3 ups_monitor.py --check-config
sudo python3 ups_monitor.py --dry-run
```

預期 I²C 位址為 `43`。dry-run 仍會寫入 INA219 的設定及校正暫存器，但不關機。請勿同時執行官方範例或其他 INA219 監控程式。先確認拔掉充電電源時為負電流／放電，接上時在充電階段為正電流／充電；滿電附近顯示狀態不明屬可能情況。

## 部署及執行

完成上述量測確認後：

```bash
sudo install -d -m 755 /opt/ups-monitor
sudo install -m 644 ups_monitor.py /opt/ups-monitor/ups_monitor.py
sudo install -m 644 ups-monitor.ini /etc/ups-monitor.ini
sudo install -m 644 ups-monitor.service /etc/systemd/system/ups-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now ups-monitor.service
sudo systemctl status ups-monitor.service
journalctl -u ups-monitor.service -f -o cat
```

預設啟用開機背景監控；若不需要，改用手動執行，不執行 enable 指令。已啟用時可停用：

```bash
sudo systemctl disable --now ups-monitor.service
sudo python3 /opt/ups-monitor/ups_monitor.py --config /etc/ups-monitor.ini
```

前景模式顯示於目前終端機。背景模式輸出至 journal，SSH 使用者可用上述 journalctl 查看即時電量與每秒倒數。關機倒數開始、10 秒、5 秒及 0 秒另使用 wall 廣播給已登入且允許訊息的終端機；wall 並不保證每個 SSH 工作階段都能看到。

## 設定

```bash
sudo nano /etc/ups-monitor.ini
sudo python3 /opt/ups-monitor/ups_monitor.py --config /etc/ups-monitor.ini --check-config
sudo systemctl restart ups-monitor.service
```

| 參數 | 預設 | 說明 |
|---|---|---|
| low_percent | 20 | 電量估算值 ≤ 此門檻時確認低電量 |
| countdown_seconds | 30 | 確認後倒數秒數，可設 0 |
| battery_only | yes | 僅在電流顯示放電時啟動；no 允許其他狀態啟動 |
| cancel_on_recovery | yes | 充電或電量 ≥ recovery_percent 時取消；no 表示觸發後持續倒數 |
| recovery_percent | 23 | 電量取消門檻，須高於 low_percent，提供遲滯 |
| low_samples | 3 | 連續低電量有效讀值次數，避免單次壓降觸發 |
| current_deadband_ma | 10 | ±10 mA 內為狀態不明，避免零點雜訊 |
| poll_seconds | 1 | 取樣週期，須大於 0 且不超過 1 秒 |
| status_seconds | 10 | 一般電量日誌週期 |
| bus / address | 1 / 0x43 | I²C 匯流排及 INA219 位址 |
| empty_voltage / full_voltage | 3.0 / 4.2 | 官方線性估算法的 0%／100% 電壓 |

各設定組合獨立生效：若希望充電時低電量也可啟動倒數，須同時設定 battery_only=no、cancel_on_recovery=no。設定只在啟動時載入；重啟服務會重新開始低電量確認及倒數。

## 異常與限制

- 電量為 `(電壓 - 3.0) / 1.2 × 100` 的限制範圍估算值，並非真正剩餘容量；負載、溫度、老化及充電會影響結果。預設 20% 約等於 3.24 V，是否有充足關機時間必須實測。
- INA219 量測電池電流，不能直接讀取外部供電存在訊號。只有正電流能作為充電證據，無法保證辨識接上電源但未充電的狀態。零電流不取消既有倒數；若需要精確的外部供電偵測，須另確認硬體訊號。
- I²C 錯誤、設定被重設、溢位或不合理電壓會停止倒數並重試。預設恢復後重新確認；cancel_on_recovery=no 時保留已觸發狀態，恢復有效量測後重開完整倒數。持續量測失敗時不會盲目關機，也無法提供低電量保護。
- 關機指令失敗會記錄錯誤並重試；成功提出 poweroff 後不再提供取消功能。
- systemctl poweroff 是作業系統關機，不保證 HAT 切斷供電或自動重新開機；關機後仍可能持續耗電。
- 日誌時間使用 Raspberry Pi 本機時區並包含 UTC 偏移；倒數使用單調時間，不受校時影響。需要臺灣時間時可用 `sudo timedatectl set-timezone Asia/Taipei`。
- systemd 服務以 root 執行以讀取 I²C 並執行關機。部署檔案由 root 管理，請勿讓一般使用者任意改寫。

## 驗證與實機驗收

軟體檢查指令：

```bash
python3 -m py_compile ups_monitor.py test_ups_monitor.py
python3 ups_monitor.py --check-config
python3 -m unittest -v test_ups_monitor
```

測試涵蓋倒數、充電／電量恢復取消、不取消設定、未知電流狀態、I²C 中斷後重新確認、模擬暫存器轉換與溢位、dry-run 不關機及關機指令呼叫。模擬測試會攔截系統指令，不會真的關機。

尚需在實機確認：I²C 讀值與電流方向、電量門檻及電池放電曲線、充電恢復取消、SSH 提示、服務開機啟動、真實 poweroff、關機完成前供電是否足夠。先停止背景服務，以獨立測試設定檔搭配 --dry-run 測試倒數與取消，再儲存工作並進行一次真實關機測試。

## 修改摘要與來源

新增獨立 Python 驅動、政策控制、設定檔、systemd 服務及模擬測試。保留原始下載的 UPS_HAT_C/INA219.py 供比對，未修改其內容。新驅動採官方實際使用的校正值 26868、電流倍率 0.1524 mA/bit 及設定 0x0EEF；16 位元負數採減 65536 正確解碼。

- 官方文件：https://www.waveshare.com/wiki/UPS_HAT_(C)
- 官方原始範例：https://files.waveshare.com/upload/4/40/UPS_HAT_C.7z

實際執行結果見 TEST_REPORT.md。未完成實機驗證前，不應視為已驗證的電池保護系統。

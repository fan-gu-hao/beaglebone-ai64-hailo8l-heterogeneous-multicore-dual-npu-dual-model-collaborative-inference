# 操作、部署與重建 / Operations, deployment and reproduction

[首頁 / Home](../README.md)

## 目前已部署的板子 / Existing deployment

先保持秤台空載，再開機；自動歸零需要暖機與取樣。網頁位於 `http://<板子IP>:8080`。目前固定使用 Logitech C270 的裝置識別路徑，並非每次開機猜測 `/dev/video0`。

Boot with an empty platform and allow warm-up/tare. The camera is selected by its validated persistent device ID. The browser endpoint is port 8080.

```bash
sudo python3 -u "$HOME/interaction_demo/service-current/service_support.py" status
```

這項健康檢查核對服務、兩個遠端核心、有效感測資料及不同時間取得的影格編號。可見關節數不是固定 17；畫面遮擋或人物不完整時會降低。

The health check verifies services, remote-core states, fresh sensor data and advancing frames. Visible-joint counts depend on pose confidence and visibility; 17 is not a required live count.

清空秤台後歸零 / Re-tare with the platform empty:

```bash
sudo systemctl reload bbai64-interaction-sensors.service
```

查看紀錄 / Inspect logs:

```bash
sudo journalctl -b -u bbai64-interaction-sensors.service \
  -u bbai64-interaction-vision.service -n 80 --no-pager
```

需要重新啟動整套板子 / Restart the board:

```bash
sudo reboot
```

感測服務 `Restart=no`，因為這套 R5F 韌體與系統組合曾有暖重啟錯誤。影像服務可在失敗後重試，但有次數限制。不要用反覆停止／啟動 R5F 取代錯誤排查。C71 客戶端離開時保留核心服務，後續可重新連接。

The sensor service avoids automatic restart loops because warm R5F stop/start failures were observed. Vision uses bounded retries. C71 remains running after a client closes and supports reattachment.

## 系統依賴 / System dependencies

| 位置 / Location | 需求 / Requirement |
|---|---|
| 板子 / Board | BeagleBone AI-64、Debian 12、核心 6.12.24-ti-arm64-r43、已驗證 DDR／MSMC 保留 |
| 推論 / Inference | Hailo-8L PCIe 驅動與 HailoRT 4.23.0、所需共享函式庫 |
| Python | 系統 Python 3.11、NumPy、OpenCV、python3-libgpiod 1.6.3 |
| 感測 / Sensors | HX711＋荷重元、MLX90614；P9.12／P9.15 GPIO；P9.19／P9.20 I2C |
| 攝影機 / Camera | 已配置的 C270；相機識別字串在 `service_support.py` |
| 主機 / Host | Ubuntu x86_64、兩個模型的獨立虛擬環境、TI SDK 與對應編譯器 |

GPIO 與 I2C 接線、電壓及腳位多工依已驗證板端配置；本文件不假設可以直接套用到其他 BeagleBone 型號。參考板端裝置樹與接線配置，勿僅依 `gpiochip` 列舉順序識別控制器。

Pinmux and electrical connections belong to the validated AI-64 setup. Do not apply these mappings to another BeagleBone model or rely on gpiochip enumeration order.

## 板端產物契約 / Runtime artifact contract

下列是現行程式實際讀取的位置。模型與韌體未包入 Git；完整雜湊位於 [artifact-manifest.json](../provenance/artifact-manifest.json)。

These are the actual paths consumed by the validated code. Binaries are external to Git; the artifact manifest records their identity.

| 板端路徑 / Board path | 內容 / Content |
|---|---|
| `/home/debian/interaction_demo/service-current/` | 服務版本連結；九個 Python 程式 / Service-version symlink |
| `/home/debian/interaction_demo/hailo_worker` | 原生 Hailo C++ 工作程式 / Native worker |
| `/home/debian/interaction_demo/models/yolov8n_custom_hailo8l-317500776eaf.hef` | 本次自行編譯的 HEF / Compiled detector |
| `/home/debian/hx711-mcu2_0.elf` | R5F 感測韌體 / Sensing firmware |
| `/home/debian/c71-pose-v2.xe71` | C71 模型與服務 / Pose firmware |
| `/home/debian/c71-pose-v2-build.json` | 對應韌體建置資訊 / Firmware manifest |
| `/home/debian/c71_pose_selftest.py` | C71 客戶端重用的 RPMsg 支援 / Shared RPMsg support |
| `/home/debian/pose_demo/` | C71 程序鎖與執行紀錄 / Locks and runtime records |
| `/home/debian/interaction_demo/scale_roi.json` | 當前攝影機的秤台區域 / Camera-specific platform ROI |

`evidence/board/config/scale_roi.json` 是拍攝當下的實例；攝影機改變位置後必須重新框選。`active-model.json` 是先前模型部署紀錄，最終系統服務的入口為 `interaction_actions.py`，不能只按該歷史檔案的 `entry` 欄位啟動。

The saved ROI is an example for that camera placement. The historical active-model manifest predates the final service entry point; the systemd vision entry is `interaction_actions.py`.

## 重建路徑與現有限制 / Reproduction path and current limits

本倉庫是獨立的實作來源與證據集合，**沒有宣稱在空白電腦複製後即可一鍵完成所有 SDK 安裝、資料收集與裝置樹修改**。腳本保留原工作目錄與檢查點，以利與既有成果對照；不應刪除雜湊檢查來強行繼續。

This repository is a standalone implementation/evidence collection, not a clean-machine installer. Scripts preserve original workspace and checkpoint assumptions. Do not remove integrity gates merely to force a later stage to run.

建議依下列順序重建 / Rebuild in this order:

1. **環境 / Environment**：取得與版本表相符的 TI SDK、工具鏈及 Hailo DFC；依 `model-pipelines/*/environment/` 建立獨立環境。這些清單是紀錄，不是通用安裝鎖定檔。
2. **模型 / Models**：依兩條模型文件重新匯出與校正。自行收集影像，保留校正／驗證分離。未公開原拍攝照片，因此重新校正的模型雜湊可能不同。
3. **韌體 / Firmware**：將 `firmware/*/pdk-overrides/` 套用到各自的 SDK 副本。主機階段腳本位於 `model-pipelines/pose/`；它們依賴上一階段報告、模型封裝及既有 PDK 工作樹。重新產生模型資產後更新相符的建置報告與完整版本檢查。
4. **板級配置 / Board configuration**：建立與 C71 連結配置相符的裝置樹記憶體保留，重啟後查核；本次生成的 DTB 與一次性修改腳本未在來源包中，僅保留配置記錄。不能從位址表直接推定新板已準備好。
5. **板端執行 / Runtime**：備妥上表產物與驅動。先完成 Hailo 串流及 C71 固定影像、外部傳圖比較，再啟動整合畫面。
6. **開機服務 / Autostart**：使用已收錄的安裝器與其手動程式基準。安裝器是原配置的精確版本，會核對產物 SHA256、已存在檔案及服務狀態；新產物需先完成相同驗證再產生匹配設定。

The original host roots are `~/hailo-model-build/yolov8n-v1` and `~/ti/bbai64-bringup/pose-mobilenetv2-v1`. Pose firmware builds additionally depend on the TI SDK beneath `~/ti/bbai64-bringup/sdk-08.00.00.12/`. Generated reports retain these paths as provenance. The generated `bb_pose_assets.c` is intentionally absent: it embeds model/test-image bytes and must be regenerated from valid local inputs.

### 原生 Hailo 工作程式 / Native worker

在已有 HailoRT 標頭與程式庫的板子，可先編譯到新位置，不覆蓋正在運行的檔案：

On a provisioned board, compile to a separate path before replacing any running worker:

```bash
mkdir -p build
c++ -O2 -std=c++17 native/hailo_worker.cpp \
    -I/usr/local/include -L/usr/local/lib \
    -lhailort -pthread -o build/hailo_worker
```

依系統實際安裝位置調整標頭／連結路徑；此文件中的命令不會自動安裝驅動或啟動服務。

Adjust include/library locations to the HailoRT installation. This command does not install drivers or start services.

### 收錄內容驗證 / Source verification

不需硬體或廠商 SDK / No hardware or vendor SDK required:

```bash
python3 tools/check_repository.py
```

功能回歸測試只需 NumPy / Logic regression tests require only NumPy:

```bash
python3 -m unittest discover -s tests -v
```

測試只驗證主機端邏輯與封裝，不代表重新執行硬體推論或重新通過模型精度測試。

These checks verify host logic and packaging, not new hardware-inference or model-accuracy results.

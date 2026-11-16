# 第三方來源與授權註記 / Third-party sources and notices

此專案包含自訂整合程式，以及從既有 TI SDK 專案保留／修改的檔案。各檔案中的原始版權、條件與免責聲明均保留；沒有用單一根目錄授權覆蓋所有第三方內容。

This repository contains project-specific integration code and retained/modified TI SDK sources. Original file-level copyright, conditions and disclaimers are preserved. No blanket repository license overrides third-party terms.

| 元件 / Component | 使用方式 / Use | 來源 / Source |
|---|---|---|
| TI Processor SDK RTOS / PDK | IPC 範例、建置與連結配置；自訂 R5F/C71 服務基礎 / IPC and build integration | [Texas Instruments](https://www.ti.com/tool/PROCESSOR-SDK-J721E) |
| TIDL、MMALIB、SYS/BIOS、TI 編譯器 | 外部建置／執行依賴，未附完整 SDK 或函式庫 / External dependencies, not bundled | 對應 SDK 安裝包與其授權 / SDK distribution and its notices |
| Hailo DFC | 外部模型轉換工具，未附安裝包 / External model compiler | [Hailo Developer Zone](https://hailo.ai/developer-zone/) |
| HailoRT | 外部 C++ 執行介面 / External runtime API | [hailo-ai/hailort](https://github.com/hailo-ai/hailort) |
| Ultralytics YOLOv8 | 原始模型載入、匯出與人物框產生 / Checkpoint loading/export/detection | [ultralytics/ultralytics](https://github.com/ultralytics/ultralytics) |
| MMPose 0.29.0 / MMCV | 姿態模型、權重載入與前處理參考 / Pose model and preprocessing reference | [open-mmlab/mmpose](https://github.com/open-mmlab/mmpose/tree/v0.29.0) |
| OpenCV、NumPy、PyTorch、ONNX、ONNX Runtime | 外部影像、數值與模型工具 / External processing libraries | 各專案原始來源與安裝套件 / Upstream projects and installed distributions |

TI 覆寫檔位於 `firmware/*/pdk-overrides/`；它們不是本專案自行重寫的驅動堆疊。部分檔案保留未使用分支或歷史 IPC 測試支援，用於對照當時建置來源。使用與散布第三方 SDK、權重或編譯產物時，應依各來源所附條款處理。

TI overrides are not an independently authored driver stack. Some conditional branches and historical IPC helpers remain to preserve source correspondence. Third-party SDKs, weights and generated artifacts retain their respective source terms.

原始權重、校正影像、完整 TI／Hailo SDK、已連結的韌體及模型二進位未隨本倉庫散布。來源連結和雜湊是追溯資訊，不是對第三方內容授予新的授權。

Checkpoints, calibration images, complete vendor SDKs, linked firmware and compiled models are not distributed here. Links and hashes provide provenance, not new permissions over upstream content.

本專案自訂程式目前未指定額外的統一開源授權；公開展示原始碼不代表所有檔案皆適用 MIT、Apache 或其他特定授權。

No additional uniform open-source license has been selected for project-specific code. Public source visibility does not imply that all files are MIT-, Apache- or otherwise uniformly licensed.

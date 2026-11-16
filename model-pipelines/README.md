# 模型與主機建置程式 / Models and host-side builds

此處保存成功開發流程中的實際腳本。模型轉換與 SDK API 說明見[模型文件](../docs/model-pipelines.md)，環境與重建條件見[操作文件](../docs/operations.md)。

These are the actual development scripts. Read the pipeline and reproduction documents before execution; paths and prerequisite reports are intentionally preserved.

| 順序 / Stage | Hailo | TI pose |
|---|---|---|
| 1 | `export_yolov8n.py` | `export_pose.py` |
| 2 | `parse_yolov8n.py` | `check_tidl_pose_fp32.py` |
| 3 | `prepare_hailo_data.py` | `prepare_pose_data.py detect` / `crop` |
| 4 | `optimize_yolov8n.py check` / `quantize` | `build_tidl_pose_int8.py` |
| 5 | `evaluate_yolov8n.py fp` / `quantized` / `report` | `evaluate_tidl_pose.py` |
| 6 | `compile_yolov8n.py` | C71 runtime and firmware build scripts |

Hailo `dfc-python` 是指定虛擬環境、CUDA 程式庫及 GPU 記憶體成長行為的啟動腳本。TI 韌體建置使用 `prepare_c71_runtime.py`、`build_c71_tidl_link.py`、`build_c71_pose_selftest.py`、`resume_c71_selftest.py`、`prepare_c71_v2.py` 等階段工具；早期測試版的產物雜湊是後續檢查點的一部分。

The Hailo launcher configures the dedicated Python/CUDA environment. TI host scripts progress through API compilation, linking, fixed-image validation and V2 transport. Earlier firmware hashes are explicit prerequisites, not interchangeable filenames.

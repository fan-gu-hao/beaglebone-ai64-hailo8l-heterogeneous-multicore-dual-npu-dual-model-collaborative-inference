# 模型轉換與執行 / Model conversion and execution

[首頁 / Home](../README.md)

## Hailo-8L：物件偵測 / Object detection

| 階段 / Stage | 檔案 / File | 使用介面 / API or tool | 工作 / Operation |
|---|---|---|---|
| 原始權重 / Checkpoint | `yolov8n.pt` | Ultralytics `YOLO`、PyTorch | 載入預訓練模型 / Load pretrained model |
| 通用圖 / Export | `yolov8n.onnx` | `YOLO.export`、ONNX、ONNX Runtime | FP32、opset 11、靜態輸入；與 PyTorch 比對 / Export and numerical comparison |
| 解析 / Parse | `yolov8n_hailo8l.parsed.har` | `hailo_sdk_client.ClientRunner.translate_onnx_model`、`save_har` | 擷取六個偵測分支，轉成 Hailo 網路表示 / Parse six detection branches |
| 浮點設定 / Native optimization | `yolov8n_hailo8l.fp.har` | `load_model_script`、`optimize_full_precision` | 載入正規化、分數分支與 NMS 設定 / Configure preprocessing and postprocessing |
| 量化與微調 / Quantization | `yolov8n_hailo8l.quantized.har` | `ClientRunner.optimize` | 校正量化範圍、量化感知微調與雜訊分析 / Calibrate, fine-tune and analyze |
| 硬體編譯 / Compile | `yolov8n_custom_hailo8l.hef` | `ClientRunner.compile` | Hailo-8L 資源配置與三個執行脈絡 / Map and compile three contexts |
| 板端推論 / Runtime | HEF | HailoRT C++ `Hef`、`VDevice`、`InferVStreams` | 載入、輸入串流、推論及偵測後處理 / Load and execute |

函式實際呼叫位置以收錄的[解析](../model-pipelines/hailo/parse_yolov8n.py)、[最佳化](../model-pipelines/hailo/optimize_yolov8n.py)、[編譯](../model-pipelines/hailo/compile_yolov8n.py)及[原生工作程式](../native/hailo_worker.cpp)為準。Python 程式自行處理工作流程、檔案驗證及比較報告；模型解析、最佳化與裝置編譯使用廠商函式庫。

The project scripts orchestrate conversion, integrity checks and reporting. Parsing, model optimization and hardware compilation use vendor APIs; they are not independently implemented compiler algorithms.

### 校正、量化與微調的關係 / Calibration and fine-tuning

校正照片先做與推論一致的 RGB 與補邊，儲存為 NHWC、float32 陣列，但**數值仍是 0～255**。float32 是校正陣列的容器型別，不代表已除以 255。模型設定中的 `normalization([0,0,0],[255,255,255])` 執行正規化，板端傳入 UINT8 影像。

Calibration uses RGB letterboxed images stored as float32 NHWC arrays with values still in 0–255. The model's normalization command divides by 255. The runtime input is UINT8.

DFC 使用 1024 張校正影像，設定最佳化等級 2、壓縮等級 0、每批一張；`post_quantization_optimization(finetune, ... epochs=4)` 透過量化模擬與浮點參考的差異調整權重。畫面中的 `_distill_loss_` 是微調的比較損失，不是新增的物品類別。整個最佳化呼叫完成後再保存量化 HAR。

DFC calibrates on 1,024 images with optimization level 2, compression level 0 and batch size 1. Four fine-tuning epochs compensate for quantization effects using distillation losses against the floating reference. The quantized HAR is saved after the optimization workflow. This is not conversion from INT32 input to FP16.

另外 128 張影像用於比較量化影響，未加入這次校正。這些照片沒有人工真值標註，因此比較的是轉換前後的一致性，不是 mAP。

The separate 128-image split evaluates conversion consistency. Without ground-truth annotations, these comparisons are not mAP measurements.

### 後處理 / Postprocessing

六分支依步距 8／16／32 對應框分布與類別分數。匯入模型設定加入類別 sigmoid，以及 YOLOv8 解碼與 NMS 描述；`engine=cpu` 指定 HailoRT CPU 後處理路徑。最終 HEF 介面為 UINT8 NHWC 640 × 640 × 3 輸入與 FLOAT32、依類別輸出的偵測結果。

The six heads are paired by strides 8, 16 and 32. Class sigmoid and YOLOv8 decode/NMS are configured in the model script. With `engine=cpu`, the HEF carries the description and HailoRT performs the selected CPU postprocessing; these operations are not all executed on the accelerator.

## TI C71／MMA：人體姿態 / Human pose

| 階段 / Stage | 產物 / Artifact | 工具 / Tool |
|---|---|---|
| 原始模型 / Original | `mobilenetv2_coco_256x192-d1e58e7b_20200727.pth` | MMPose 0.29.0、MMCV、PyTorch |
| 匯出 / Export | `mobilenetv2_pose_256x192.fp32.onnx` | PyTorch ONNX 匯出、ONNX Runtime 比對 |
| 浮點驗證 / FP32 check | TI 浮點網路與輸出 | `tidl_model_import.out`、`PC_dsp_test_dl_algo.out` |
| 校正 / Calibration | 64 張校正人物、32 張驗證人物 | YOLO 人物框、MMPose 相容仿射裁切 |
| INT8 匯入 / Import | TIDL 網路 `.bin`、IO 描述 `.bin` | `tidl_model_import.out`、校正與圖編譯 |
| 裝置規劃 / Target mapping | 網路內的資料流配置 | `ti_cnnperfsim.out` |
| 韌體連結 / Firmware link | `c71-pose-v2.xe71` | TI C7000 編譯器、PDK IPC、SYS/BIOS、TIDL、MMALIB |
| 執行 / Runtime | 17 關節 | C71 服務、MMA、RPMsg 客戶端 |

TIDL 設定使用 8 位元參數與特徵、`quantizationStyle=2`、`calibrationOption=7`、10 次偏差校正迭代，以及裝置圖編譯。這條流程與 Hailo 的四輪量化感知微調不同，沒有把 Hailo 設定直接套到 TI。

The TIDL path uses 8-bit parameters/features, quantization style 2, calibration option 7, ten bias-calibration iterations and target graph compilation. It is distinct from Hailo's four-epoch quantization-aware fine-tuning.

TIDL 原生網路描述與 IO 結構受 SDK 版本約束，不能任意混用新版模型與舊版執行函式庫。C71 服務查核維度、型別、填補與間距，完成 `algAlloc`、`algInit` 及推論呼叫。輸出為 17 × 64 × 48；TI 輸出縮放值 169.510345 對應反量化步距約 0.005899345。

Native TIDL network and IO structures are SDK-version dependent. The runtime validates dimensions/types/padding, allocates the algorithm, initializes it and executes inference. Its output scale of 169.510345 corresponds to an inverse step of approximately 0.005899345.

## 重要版本與產物雜湊 / Versions and artifact identity

完整版本見兩個 `environment/` 目錄；產物雜湊見 [artifact-manifest.json](../provenance/artifact-manifest.json)。主要工具組合：

| 工作 / Task | 版本 / Version |
|---|---|
| Hailo 匯出 / Export | Python 3.12.3、Ultralytics 8.3.161、PyTorch 2.5.1+cpu、ONNX 1.16.2 |
| Hailo DFC | Python 3.10.19、DFC 3.33.1、TensorFlow 2.18.0 |
| Hailo 板端 / Board | HailoRT 4.23.0、Debian 12、OpenCV 4.6.0 |
| TI SDK / TIDL | SDK 08.00.00.12／TIDL 08.00.00.10 |
| TI 編譯器 / Compilers | ARM 20.2.0.LTS、C7000 1.4.2.LTS |
| 姿態來源 / Pose source | MMPose 0.29.0，commit `0d9a00c1d64b68999e4b2a4ee5ed0d5ac2c3e296` |

環境清單記錄成功時的套件，不等同保證任意作業系統可直接安裝的鎖定檔。GPU 僅在筆電轉換／最佳化時使用；展示推論於板上的 Hailo 與 TI 加速單元執行。

Environment manifests record the successful installation, not universally portable lockfiles. The laptop GPU supports conversion/optimization; deployed inference runs on the board's Hailo and TI accelerators.

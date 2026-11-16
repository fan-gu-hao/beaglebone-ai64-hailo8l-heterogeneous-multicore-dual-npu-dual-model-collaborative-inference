# 實測結果與證據 / Measurements and evidence

[首頁 / Home](../README.md)

## 證據類型 / Evidence types

- **原始報告 / Original reports**：`evidence/` 內從開發環境收錄的 JSON；`provenance/source-manifest.json` 保留原文雜湊。
- **終端紀錄摘要 / Terminal record summary**：以下 Hailo 串流及 C71 外部影像結果來自開發過程提供的終端輸出，沒有將手動整理文字冒充新產生的機器測試報告。
- **倉庫檢查 / Repository checks**：本倉庫提供來源校驗、語法／連結檢查及主機邏輯測試。它們不取代板端測試。

Original JSON reports are preserved verbatim. Hardware figures summarized from supplied terminal output are identified as such; repository checks are separate and do not imply a new board run.

## Hailo

| 測試 / Test | 結果 / Result |
|---|---|
| PyTorch 與 ONNX／bus | 框最大差 0.00396729 像素；分數最大差 0.00000472 |
| PyTorch 與 ONNX／zidane | 框最大差 0.00317383 像素；分數最大差 0.00000748 |
| 編譯 / Compilation | HAILO8L、3 contexts、9,403,484 bytes、221.6 s |
| 板端串流 / Board streaming | 702 frames，139.45 frames/s，約 5 s |

來源 / Sources：[匯出報告](../evidence/hailo/export-report.json)、[編譯報告](../evidence/hailo/compile-report.json)；串流數字來自 `hailortcli run --time-to-run 5 --batch-size 1` 的提供紀錄。

128 張分離影像的轉換比較，浮點與量化顯示門檻皆 0.35、同類框 IoU 至少 0.5 / Comparison on 128 held-out images, score 0.35 on both models and same-class IoU ≥ 0.5:

| 類別 / Class | 浮點框 / FP | 量化框 / Quantized | 配對 / Matched | 浮點框保留率 / FP retention |
|---|---:|---:|---:|---:|
| 人 / Person | 121 | 122 | 117 | 96.7% |
| 瓶子 / Bottle | 6 | 6 | 6 | 100.0% |
| 杯子 / Cup | 9 | 8 | 6 | 66.7% |
| 手機 / Phone | 33 | 24 | 23 | 69.7% |

降低量化顯示門檻至 0.25 時，手機配對提升為 32/33；這屬於分析結果，**本次收錄的原生執行程式仍採用 0.35 初篩，融合分類另用 0.45**。未配對框不一定是誤判，浮點模型也不等於人工真值。瓶子與杯子樣本數小，不能據此宣稱一般化準確率。

Lowering the quantized threshold to 0.25 recovered 32/33 phone boxes in the analysis. This is not the deployed threshold: the captured native worker uses 0.35 and fusion uses 0.45. Neither unmatched boxes nor floating-point predictions establish ground truth.

## TI 與 C71 / TI and C71

浮點 ONNX 與 TI 主機模擬的三組數值比對最大差小於 8 × 10⁻⁷；灰階及隨機輸入用來查核算子／數值相容性，不衡量真實姿態品質。

Three grey/random-input comparisons show maximum FP32 differences below 8 × 10⁻⁷. These test numerical compatibility, not pose semantics.

量化後實拍裁切的報告保留以下結果 / Quantized real-crop report:

| 範圍 / Group | 浮點可信點 / FP confident | 量化可信點 / Quantized confident | 位移 ≤ 8 px / Within 8 px | 平均位移 / Mean shift |
|---|---:|---:|---:|---:|
| 全部關節 / All | 365 | 316 | 280 | 6.923 px |
| 肩肘腕 / Upper-limb | 162 | 147 | 122 | 7.389 px |
| 手腕 / Wrists | 56 | 48 | 44 | 6.603 px |

原始[量化驗證報告](../evidence/pose/pose-validation-report.json)的 `quality_accepted=false`、`ti_hardware_verified=false` 是**當時階段的狀態**，保留原樣。後來板端成功執行可證明硬體流程完成，不能倒推先前品質門檻已全部通過。姿態平滑改善顯示穩定性，也不等於修正所有關節定位誤差。

The report's false acceptance/hardware flags describe that earlier phase and are not rewritten. Later hardware execution establishes deployment, not retrospective passage of all accuracy gates. Temporal smoothing does not remove every localization error.

後續提供的板端紀錄 / Subsequent supplied board records:

- 單張參考：讀回全部 52,224 個 INT8，反量化最大差 `5.960464477539063e-08`，17 個熱圖峰值座標皆相同。
- 外部原圖、左右翻轉、恢復原圖三次推論完成；原圖重現與 TI 參考一致。
- 傳圖 37.7～38.0 毫秒；推論請求往返 7.6～7.7 毫秒。
- 即時整合時觀察傳圖約 42～57 毫秒、請求往返約 7.8～10 毫秒；受整體工作負載影響。

The full reference output and original/flip/original tests validate transport and repeatability. Reported request latency includes protocol/service overhead. No isolated MMA kernel benchmark or energy measurement is claimed.

## 感測與開機 / Sensing and boot

HX711 早期三段測試中，空秤中位數 -186722、載入 -142206、卸載 -186679；載入差 44516、卸載偏移 43。這是原始計數的存在感測測試，不是重量校正。R5F 回報時脈脈衝最大計時 5 微秒。

The initial load test measured raw-count separation and return-to-zero, not calibrated mass. Firmware-reported pulse timing reached 5 µs in the supplied run.

最新收錄的[開機健康報告](../evidence/board/boot-validation.json)確認感測／影像服務 active、R5F／C71 running、溫度與有無資料有效，以及兩次讀取之間影格有前進。顯示速度為 9.7 張／秒；其他操作紀錄約 9.9～10.1 張／秒。

The collected boot report verifies both services, both remote cores, valid sensor data and frame advancement. It reports 9.7 frames/s; other demonstration observations were approximately 9.9–10.1 frames/s.

## 尚未主張的能力 / Claims not made

沒有完整 COCO AP／mAP 評估、多人姿態追蹤、物品接觸真值、精密重量校正、溫度與影像空間配準、全時段耐久性或跨版本 SDK 相容保證。下一階段展示影片應呈現目前已測到的行為，並保留這些範圍限制。

This release does not claim full COCO evaluation, multi-person tracking, contact ground truth, precision mass measurement, spatial thermal registration, long-duration qualification or cross-SDK compatibility.

# 遠端核心韌體 / Remote-core firmware

- [`r5f/pdk-overrides/`](r5f/pdk-overrides/)：HX711 mcu2_0，FreeRTOS，建置目標 `ipc_echo_test_freertos`。
- [`c71/pdk-overrides/`](c71/pdk-overrides/)：TIDL c7x_1，SYS/BIOS，建置目標 `ipc_echo_test_tirtos`。
- [`c71/tools/`](c71/tools/)：TIDL 介面與模型描述檢查工具。

兩份覆寫目錄屬於不同工作樹，不能合併後同時覆寫同一份 PDK。請保留完整 SDK，將相對 `ti/` 路徑套用到對應 `pdk/packages/`；此處只收錄本次目標需要的來源，非完整 PDK。

The R5F and C71 overrides belong to separate worktrees. Do not merge both into one PDK tree. Apply relative `ti/` paths to the respective `pdk/packages/` copy of the complete SDK.

C71 程式已是 V2 外部傳圖版本，`bb_pose_transport_v2.inc` 亦已收錄。`bb_pose_assets.h` 宣告模型／測試資產，生成的 `.c` 大型陣列未附；由本機模型封裝與測試資料重新產生。主機建置腳本位於 [`model-pipelines/pose/`](../model-pipelines/pose/)，是含既有工作樹與產物驗證的階段式腳本，不能假設在此目錄直接執行即可完成全新建置。

C71 contains the final V2 external-image transport. Generated binary asset arrays are omitted and must be regenerated. Host scripts retain their phase/workspace dependencies; they are not a standalone clean build command in this directory.

本次目標以外的 A72、C66、其他 R5F／作業系統配置未收錄，排除清單與 SHA256 位於 [`source-manifest.json`](../provenance/source-manifest.json)。

Unused processor/OS variants are excluded and listed with hashes in the provenance manifest.

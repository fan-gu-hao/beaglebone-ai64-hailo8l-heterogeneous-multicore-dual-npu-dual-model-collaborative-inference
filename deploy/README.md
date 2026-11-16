# 開機服務 / Boot services

`validated-units/` 保存當時的三個系統服務檔案；其路徑指向已驗證版本 `service-v1.5m70koqx`。這些檔案是部署證據，不能不更改版本路徑就直接安裝到另一台設備。

The captured units refer to the validated version directory. They are deployment evidence, not path-independent templates.

安裝器在 [`bringup/board/install_interaction_boot.py`](../bringup/board/install_interaction_boot.py)，輸入程式在 [`manual-baseline/`](../bringup/board/manual-baseline/)。安裝器會建立新的服務副本、保留舊設定並核對模型／韌體版本。已運行的系統不需要為了發布 GitHub 重裝。

The installer creates a versioned service copy and checks the expected model/firmware identities. Publishing this repository does not require reinstalling the working board.

感測服務：自動空秤歸零、裝置就緒、SIGUSR1 重新歸零、不自動重啟。影像服務：等待感測服務、檢查 Hailo／攝影機、有限次數的失敗重試。應用紀錄每檔 8 MiB、保留三份輪替備份，服務輸出送至系統日誌。

Sensing performs startup tare and signal-driven re-tare without automatic restarts. Vision uses device readiness and bounded retries. Application log files rotate at 8 MiB with three backups; service output goes to the journal.

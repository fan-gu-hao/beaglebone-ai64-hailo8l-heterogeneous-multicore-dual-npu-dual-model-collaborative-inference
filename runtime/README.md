# 板端應用 / Board application

此目錄九個 Python 檔案由正在使用的開機服務版本直接收錄，未因 GitHub 整理更改執行邏輯。實際入口為 `interaction_actions.py` 與 `interaction_sensors.py`；前者經由姿態、穩定與融合模組組成網頁服務。

The nine Python files are collected verbatim from the boot-validated service version. Entry points are `interaction_actions.py` and `interaction_sensors.py`.

`support/c71_pose_selftest.py` 提供 C71 用戶端使用的 RPMsg／remoteproc 輔助。其直接執行模式屬於早期固定影像自我測試，需要當時的資產與報告；目前即時服務僅匯入所需支援函式。

The support script provides RPMsg/remoteproc helpers. Its standalone entry point is an earlier fixed-image test requiring its own historical assets; the live client reuses helper functions.

目前程式保留 `/home/debian/interaction_demo` 與 `/home/debian/pose_demo` 路徑，以及已驗證的硬體識別。操作方式與可重建範圍請看[操作文件](../docs/operations.md)。

The current code retains deployment-specific paths and hardware identity. See the operations document before adapting it to another workspace or camera.

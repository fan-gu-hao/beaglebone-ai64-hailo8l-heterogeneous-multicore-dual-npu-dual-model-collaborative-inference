# 展示錄影流程 / Demonstration recording plan

[首頁 / Home](../README.md)

建議影片約 2～3 分鐘，以實際系統行為及兩條模型的部署證據為主。影片連結待完成拍攝後加入首頁，不預先宣稱已錄製。

Record approximately 2–3 minutes showing real behavior and model-deployment evidence. Add the video link after recording.

| 順序 / Order | 畫面 / Shot | 說明 / Narration |
|---|---|---|
| 1 | 板子、Hailo、攝影機、秤台、紅外線模組 / Hardware overview | 四種執行單元的分工 / Compute ownership |
| 2 | 清空秤台後 `sudo reboot`，回到網頁 / Boot and browser | 服務自動啟動，不需手動跑 Python / Automatic startup |
| 3 | 放上寶特瓶、手離開 / Place bottle and withdraw hand | 荷重確認存在，影像命名瓶子，姿態提供手腕線索 / Sensor truth plus visual context |
| 4 | 拿走瓶子，再換杯子或手機 / Remove and change object | 放置／移走事件、類別與時間 / Event transitions |
| 5 | 適度遮擋物品 / Brief occlusion | 類別未確認時仍保留有無判斷 / Presence survives uncertain classification |
| 6 | 展示溫度變化 / Surface temperature | 說明視野表面溫度範圍 / Explain field-of-view meaning |
| 7 | GitHub 架構與模型流程頁 / Repository overview | 原始權重到 HEF、TIDL 與韌體；顯示已驗證結果 / End-to-end conversion evidence |

拍攝前固定相機並重選秤台區域。人物上半身與雙手應盡量入鏡，一次只操作一個物品；無法辨識時如實保留「未確認」。畫面更新約 10 張／秒與 Hailo 獨立串流約 139 張／秒屬於不同測量，不在字幕中混用。

Fix the camera and select the ROI before recording. Keep the upper body and hands visible, handle one object at a time, and retain uncertainty messages honestly. Do not label the complete demonstration as 139 FPS based on the standalone Hailo test.

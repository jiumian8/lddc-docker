# LDDC Docker Web 版

这是基于原 LDDC GPLv3 源码增加的无头 Web 服务，保留原歌词 API/解析逻辑，不启动 Qt 桌面界面。

## 快速启动

1. 将 `docker-compose.yml` 中 `./music` 左侧改成宿主机音乐目录。
2. 执行：

```powershell
docker compose up -d --build
```

3. 浏览器打开 <http://localhost:8080>。
4. 点击“开始扫描”，服务会扫描 `/music` 下的音频和 `.lrc`。

## 规则

- 已经是逐词 LRC 的文件默认跳过。
- 普通逐行 LRC、空歌词或缺失歌词会按音频标签/文件名从 QQ 音乐、酷狗、网易云、LRCLIB 依次搜索。
- 只有拿到逐词歌词才覆盖/写入；默认不覆盖已有逐词 LRC。
- 开启 `overwrite` 或 UI 的“覆盖已有逐词歌词”后会重新刮削。
- `.lrc` 的判断依据是每行至少存在两个带时间的歌词字/词片段。

## 定时扫描

设置环境变量 `SCAN_INTERVAL_MINUTES`，例如 `360` 表示每 6 小时扫描一次。默认 compose 已配置为 360 分钟。

## GitHub 构建

`.github/workflows/docker.yml` 会在 `main` push、tag 和 PR 时构建；main/tag 自动推送到 GitHub Container Registry。将 compose 中的 `你的用户名` 替换为真实 GitHub 用户名后即可使用发布的镜像。

> 版权：原项目及新增服务均按 GPLv3 发布，原始版权和 LICENSE 文件保留。

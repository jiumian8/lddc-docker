# LDDC MUSIC

jiumian 维护的 Linux/NAS Docker Web 版，基于 [chenmozhijin/LDDC](https://github.com/chenmozhijin/LDDC) 的歌词搜索、解析与匹配相关代码。原项目的桌面功能说明见 [README.md](README.md)；Docker Web 版并不提供桌面歌词、拖拽搜索或写入音频标签等全部桌面功能。保留原项目版权声明及 [GPL-3.0 许可证](LICENSE)。

## 部署到 Linux / NAS

GitHub Actions 在向 `main` 推送时构建并发布 `ghcr.io/jiumian8/lddc-docker:latest`。在服务器新建 `compose.yaml`：

```yaml
services:
  lddc:
    image: ghcr.io/jiumian8/lddc-docker:latest
    container_name: lddc-docker
    ports:
      - "1122:8080"
    volumes:
      - "/volume2/jiumian/音乐:/music"
      # 仅使用“保存到指定目录”时需要另外挂载：
      # - "/volume2/jiumian/歌词:/lyrics"
      - "./data:/data"
    environment:
      TZ: Asia/Shanghai
      MUSIC_ROOT: /music
      STATE_DIR: /data
      ALLOWED_ROOTS: "/music,/data,/lyrics"
    restart: unless-stopped
```

将 `/volume2/jiumian/音乐` 换成实际的 Linux/NAS 音乐目录；`/music` 是 **容器内** 路径。WebUI 的扫描目录填写 `/music`，不要填写 NAS 上的宿主机路径。若要把歌词存到其它位置，取消上例中的 `/lyrics` 挂载注释，调整宿主机路径，然后在 WebUI 设置中选择“保存到指定目录”，填入 `/lyrics`。容器必须对目标目录有写权限。

```sh
docker compose pull
docker compose up -d
```

在浏览器访问 `http://NAS地址:1122`，首次打开先设置管理员密码（至少 8 位），之后登录。密码以带随机盐的 PBKDF2 哈希保存在 `/data/auth.json`；`./data` 挂载目录应只对管理员开放。不要直接把 HTTP 端口暴露到公网；如使用 HTTPS 反向代理，可额外配置 `COOKIE_SECURE: "true"`。会话默认有效期为 4 小时，容器重启后需重新登录。

## 扫描与保存

登录后可以手动开始扫描；点击右上角“设置”配置定时扫描、间隔、歌词源、匹配分、时长过滤、保存方式、格式和文件名模板。定时设置由 WebUI 保存到 `/data/settings.json`，**不再由 `SCAN_INTERVAL_MINUTES` 控制**。更改定时间隔后会重新计时。

- 目前扫描挂载目录中的音频文件（包括 FLAC、MP3 等），检查其对应的目标 `.lrc`；**不会单独刮削没有音频文件的 `.lrc`**。
- 默认保存方式是“音频旁同名保存”，例如 `/music/歌名.flac` 对应 `/music/歌名.lrc`，需要音乐挂载可写。
- “保存到指定目录”会写入 WebUI 填写的容器内路径，按文件名模板命名。支持 `%title%`、`%artist%`、`%album%`、`%filename%`；请注意：同名歌曲可能得到相同的目标文件名，必要时在模板中增加区分字段。
- 目标歌词已经是逐词时默认跳过；若目标缺失或是逐行歌词，则搜索逐词歌词并写入。勾选“覆盖已有逐词歌词”才会重新刮削已存在的逐词文件。跳过日志会显示**实际检查的完整目标路径**：删掉 `/music` 中的文件不代表 `/lyrics` 中的文件也已删除。
- WebUI 可选逐词 LRC、增强 LRC、逐行 LRC。只保存匹配到的原文歌词；获取不到逐词歌词时显示失败，不会用逐行结果冒充逐词结果。
- `@eaDir` 等群晖辅助目录会被跳过。搜索源需要容器能访问相关音乐服务。

## 记录与排查

运行日志显示带时间的成功、失败、跳过结果以及每次扫描的总数。首页展示最近 10 次扫描；最近 100 次完整任务记录保存于 `/data/scrape-history.json`，重启后不会丢失，并可在登录后通过 `GET /api/history` 查询。详细搜索与写入异常只在容器日志中：

```sh
docker compose logs --tail=200 lddc
```

如果 WebUI 显示跳过但找不到歌词，先查看跳过条目中的**容器内完整路径**及 WebUI 的保存方式，再检查 Compose 中对应的宿主机挂载。更新镜像后执行 `docker compose pull` 和 `docker compose up -d`；确保 GitHub Actions 对最新提交的构建已成功。

## 从源码构建

仓库内也提供 `Dockerfile` 和 GitHub Actions 工作流 `.github/workflows/docker.yml`。若需要在有 Docker 的 Linux 主机上本地构建，可执行：

```sh
docker build -t lddc-music:local .
```

将 Compose 中的 `image` 改成 `lddc-music:local` 后启动。CI 在推送 `main` 或版本标签时构建并推送 GHCR 镜像；PR 仅构建检查，不发布镜像。

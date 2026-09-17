# LDDC MUSIC

LDDC MUSIC 是由 **jiumian** 维护的 Linux/NAS Docker 逐词歌词管理工具，基于开源项目 [chenmozhijin/LDDC](https://github.com/chenmozhijin/LDDC) 的歌词搜索、解析和匹配能力开发。

项目提供现代化 Web 管理界面，可以扫描音乐目录、识别已有歌词是否为逐词歌词，并从 QQ 音乐、酷狗音乐、网易云音乐和 LRCLIB 搜索匹配逐词歌词。适合部署在群晖、Linux 服务器及其他支持 Docker Compose 的 NAS 上。

本项目遵循 GPL-3.0 许可证，并保留原 LDDC 项目的版权和开源声明。

## 功能简介

- 扫描指定目录中的 FLAC、MP3、M4A、OGG、OPUS、WAV、AAC、WMA、APE 等音频文件。
- 读取音频标题、歌手、专辑和时长等标签，标签不完整时可从文件名识别歌曲信息。
- 自动判断目标 `.lrc` 是否已经是逐词歌词。
- 已有逐词歌词默认跳过，并在日志中显示实际检查的完整路径。
- 缺少歌词或只有逐行歌词时，自动搜索并下载逐词歌词。
- 支持 QQ 音乐、酷狗音乐、网易云音乐和 LRCLIB 多歌词源。
- 使用标题、歌手、专辑和歌曲时长进行匹配评分。
- 支持逐词 LRC、增强 LRC和逐行 LRC 输出格式。
- 支持将歌词保存在音频文件旁，或保存到单独挂载的歌词目录。
- 支持 `%title%`、`%artist%`、`%album%`、`%filename%` 文件名模板。
- 支持手动扫描和定时自动扫描，定时开关及间隔均在 WebUI 中设置。
- 扫描日志显示时间、成功、失败、跳过和任务统计。
- 最近 100 次扫描记录持久化保存在 `/data/scrape-history.json`。
- 自动忽略群晖 `@eaDir` 辅助目录。
- 首次使用设置管理员密码，密码使用带随机盐的 PBKDF2-HMAC-SHA256 哈希保存。
- 登录采用短时 HttpOnly Cookie，会话默认有效期为 4 小时。
- 扫描和保存路径受到容器路径白名单限制，降低路径穿越风险。

## Docker Compose 安装

以下配置适用于 Linux 和 NAS。将宿主机目录改成自己的实际路径。

```yaml
services:
  lddc:
    image: ghcr.io/jiumian8/lddc-docker:latest
    container_name: lddc-music
    ports:
      - "1122:8080"
    volumes:
      # 音乐目录。若选择“音频旁同名保存”，该目录必须可写。
      - "/volume2/jiumian/音乐:/music"

      # 可选：将歌词保存到单独目录时启用。
      - "/volume2/jiumian/歌词:/lyrics"

      # 保存管理员密码哈希、WebUI 设置和扫描记录。
      - "./data:/data"
    environment:
      TZ: Asia/Shanghai
      MUSIC_ROOT: /music
      STATE_DIR: /data
      ALLOWED_ROOTS: "/music,/data,/lyrics"

      # 通过 HTTPS 反向代理访问时取消注释。
      # COOKIE_SECURE: "true"

      # 登录有效期，单位为秒；14400 为 4 小时。
      SESSION_TTL_SECONDS: "14400"
    restart: unless-stopped
```

## 启动方法

在 `compose.yaml` 所在目录执行：

```sh
docker compose pull
docker compose up -d
```

浏览器访问：

```text
http://NAS或服务器IP:1122
```

第一次打开时需要设置至少 8 位管理员密码。登录后点击右上角“设置”，建议按以下方式配置：

```text
扫描目录：/music
歌词格式：逐词 LRC
保存方式：音频旁同名保存，或保存到指定目录
指定保存目录：/lyrics
文件名模板：%title% - %artist%
歌词源：QQ音乐、酷狗、网易云、LRCLIB
最低匹配分：55
启用时长过滤：开启
定时扫描：按需开启
```

如果使用“音频旁同名保存”，歌词会保存为：

```text
/music/歌曲名称.lrc
```

如果使用“保存到指定目录”，并将保存目录设为 `/lyrics`，歌词会保存到宿主机 Compose 中映射的歌词目录。

## 更新镜像

```sh
docker compose pull
docker compose up -d
```

如果确认 GitHub Actions 已构建新版本，但服务器仍使用旧镜像，可以执行：

```sh
docker compose down
docker image rm ghcr.io/jiumian8/lddc-docker:latest
docker compose pull
docker compose up -d
```

## 查看日志

查看最近 200 行容器日志：

```sh
docker compose logs --tail=200 lddc
```

持续查看日志：

```sh
docker compose logs -f lddc
```

WebUI 仅显示简洁的成功、失败和跳过信息；详细的搜索异常和写入错误会输出到 Docker 容器日志。

## 数据文件

持久化数据位于 Compose 映射的 `./data` 目录：

```text
auth.json             管理员密码哈希
settings.json         WebUI 设置
scrape-history.json   最近 100 次扫描记录
```

请备份该目录，并限制非管理员用户访问。不要将服务的 HTTP 端口直接暴露到公网；需要远程访问时，建议使用 HTTPS 反向代理并启用 `COOKIE_SECURE`。

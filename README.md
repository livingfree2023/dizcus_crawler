# Discuz 离线存档

将 Discuz 论坛各板块的精华帖主楼保存为独立 HTML，正文和图片在一个文件中，GIF 保留动画。浏览器打开文件即可离线阅读。

同一帖子只接受首次满足条件的存档。后续改标题、改正文、换图或删除，均不会修改本地副本。精简 Web 页面提供统一的标题列表，点击即可阅读，不分板块。

[查看列表原型](docs/prototype/index.html)

## 快速开始

需要 Python **3.11 或更新版本**，使用标准库，无需 pip 安装依赖。仅支持有权访问的公开或已授权内容；支持常见 Discuz 经典版和 X 模板。

```sh
cp config.example.toml config.toml
chmod 600 config.toml
```

编辑 `config.toml`，设置论坛 `url` 和稳定的 `key`。同一论坛更换域名时保留原 `key`。首次运行扫描所有可见板块，也可以用 `fids` 指定板块编号。

```sh
python3 discuz_archive.py --config config.toml
```

完成后在 `output/` 中查看归档。每个帖子一个文件，文件名采用首次归档时的标题；同名帖子自动增加标识。

## 图片数量门槛

在配置现有的 `[archive]` 中设置，例如：

```toml
min_images = 5
```

成功获取并通过格式检查的正文图片至少 5 张时，保存正文和这些图片，失效或无权限的图片舍弃。少于 5 张则不生成 HTML，并记录永久跳过历史，以后不再请求该帖正文或图片。即使后来修复链接、增加权限或降低门槛，也不会自动重抓。

同一图片链接多次出现只计一张，GIF 动画也只计一张；回帖、签名、广告等图片不计入。已经归档的帖子不会因新阈值被删除或重新检查。

示例配置设为 1。旧配置省略此项或设为 0 时，沿用原行为：所有目标图片完整后才存档，失败可重试。无法识别正文或整页临时网络故障仍报告失败，不把未知情况误判成图片不足。

## 网页阅读

```sh
python3 discuz_archive.py --config config.toml --serve
```

打开 [本地存档列表](http://127.0.0.1:8080)。阅读时只加载本地文件，不连接源论坛。浏览器支持时可安装为独立窗口；离线时可直接打开已保存的 HTML。

服务器上建议通过 SSH 转发访问：

```sh
ssh -L 8080:127.0.0.1:8080 user@server
```

需要远程绑定时，在配置中指定 `web.token_file`，文件内容至少 24 个字符。浏览器登录用户名为 `archive`，密码为文件内容；远程访问应使用 HTTPS 反向代理。

## Debian / Ubuntu 每日运行

需要使用 systemd 的 Debian/Ubuntu，且 Python 版本至少为 3.11。将脚本与配置放在持久目录中，例如 `/opt/discuz-archive/`，运行账号需要读取脚本、配置及可选 Cookie，并可写入归档和历史目录。

先运行一次抓取，再安装每日任务：

```sh
sudo python3 discuz_archive.py --config "$PWD/config.toml" --install
```

默认每天按服务器本地时间 **03:00** 执行，关机期间错过的任务会补跑。修改配置中的 `schedule.on_calendar` 后，再运行一次 `--install` 应用新时间。默认同时启用 Web 阅读服务；设 `web.enabled = false` 可仅安装定时抓取。

服务以调用 sudo 的普通用户运行；直接以 root 安装时创建专用账号。权限预检不通过会明确停止，不会让服务以 root 抓取。

```sh
systemctl status discuz-archive.timer
journalctl -u discuz-archive.service -n 50
```

卸载：

```sh
sudo python3 discuz_archive.py --uninstall
```

卸载保留配置、存档和抓取历史。卸载不依赖原配置文件仍然存在，也可以重复执行。

## 常用设置与备份

- `forum.fids`：要扫描的板块，留空自动发现。
- `forum.cookie_file`：可选 Netscape 格式 Cookie 文件；请设置为仅本人可读。
- `archive.min_images`：可获取正文图片的最低数量，不足永久跳过。
- `request.interval`：请求间隔，默认 2 秒。
- `web.port`：网页阅读端口，默认 8080。
- `schedule.on_calendar`：systemd 每日执行时间，可指定时区。

请先暂停抓取及阅读服务，再将配置、归档和历史数据一同备份。历史丢失时程序会停止，避免误抓新版；已归档文件丢失时也不会用源站当前内容替换它。

```sh
python3 discuz_archive.py --config config.toml --check
python3 discuz_archive.py --config config.toml --install --dry-run
```

第一个命令检查本地存档完整性，第二个预览服务配置。两者都不会抓取论坛。

程序遵守 robots.txt；常规模板之外的广告元素可通过 `forum.drop_selectors` 和 `forum.ad_hosts` 补充过滤。失效、受限或格式不支持的图片不计入可获取数量，启用门槛后会继续尝试同帖其余图片，再决定保存或永久跳过。模板无法识别等无法确定数量的故障仍报告失败。

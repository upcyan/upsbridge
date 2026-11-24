# FN Connect：旧桌面页打不开，刷新后恢复

## softrouter：把桌面端口 iframe 迁到同源网关

[项目审计](</vol1/1000/deepseek_harness/softrouter/AUDIT.md#L160-L186>)区分了两个场景：调整嵌入策略能解决部分局域网问题，却不能让远程 HTTPS 桌面加载 HTTP 子帧，也不能让 FN Connect 转发应用自定义端口。最终[构建配置](</vol1/1000/deepseek_harness/softrouter/fpk/build.py#L63-L74>)使用空 protocol、固定路径及 Unix socket；[面板实现](</vol1/1000/deepseek_harness/softrouter/fpk/panel.js#L8454-L8482>)复用请求处理器，保留独立 TCP 入口。页面导航、资源、API 与鉴权跳转都适配前缀。审计记录了前缀回归和浏览器流程通过，本轮没有重跑其测试。

upsbridge 可以借鉴同源入口，但必须保留应用自身的访问边界，不应无条件信任所有本机 socket 请求。softrouter 记录的故障不是“同一入口只有旧标签页失败”的实证。

## 其他项目与本应用

现象来自用户截图，未抓到故障发生时的请求。刷新能恢复，不足以判定是权限、缓存还是后端短暂未就绪。

- KimiCode 的[桌面入口](/vol1/1000/deepseek_harness/fnos_kimicode/ui/config#L6-L11)使用固定前缀和 Unix socket。[启动脚本](/vol1/1000/deepseek_harness/fnos_kimicode/cmd/main#L81-L94)检查网关、socket 和运行时；[网关](/vol1/1000/deepseek_harness/fnos_kimicode/gateway/gateway.mjs#L165-L175)对非 assets 路径禁存，[上游不可用分支](/vol1/1000/deepseek_harness/fnos_kimicode/gateway/gateway.mjs#L245-L253)返回 502 并提示稍后重试。这是代理侧的处理，不能证明能恢复被 FN Connect 拒绝的请求。
- DSH 的[桌面入口](/vol1/@appcenter/dsh-fnos/ui/config#L6-L11)同样使用固定前缀和 socket。[网关旧页兼容](/vol1/@appcenter/dsh-fnos/gateway.js#L835-L844)有直接相关的修复：有效会话的 iframe 导航在宽限期后按路径匹配，避免刷新附加查询参数导致误拒。[维护分支](/vol1/@appcenter/dsh-fnos/gateway.js#L964-L967)返回 503、禁止缓存，提示页每五秒刷新。这只覆盖请求到达应用网关的情况。
- upsbridge 的[桌面入口](../app/ui/config#L10-L17)仍是端口 iframe，未配置网关前缀或 socket。没有依据直接移植 DSH 的会话逻辑或放宽权限。复现时先比较同一时刻旧、新标签页的 iframe 文档请求状态与响应来源，只记录路径（不含查询串）、状态和时间。若后续迁移前缀入口，需一并适配页面的根路径 API、日志链接及下载，验证旧页刷新、冷启动与升级恢复。
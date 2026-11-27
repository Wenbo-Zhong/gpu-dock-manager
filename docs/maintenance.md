# 维护约定与本次检视修复

管理器按职责分层：`hardware` 只读发现；`presence` 用实时路由纠正缓存；
`probe/worker` 隔离驱动阻塞；`core/controller` 维护状态与锁，
`core/policy` 派生策略，`core/operations` 执行设备操作；`daemon` 提供授权 API；
`client/ui/desktop` 处理用户会话。界面不能直接执行 PCI 修改。
完整目录职责和兼容入口见 [架构文档](../ARCHITECTURE.md)。

## 状态和并发

- `operation_lock` 串行化手动操作与自动策略。后台请求必须先
  `reserve_operation`，再提交线程池；只有同一预约令牌能执行或结束任务。
- `device_lock` 保证最终安全检查与设备修改位于同一临界区。
  排队移除必须把最初连接的显示器集合传到最终检查，新增或未知连接立即拒绝。
- GUI 按请求保存未完成状态；取消请求完成不能清除另一个请求的忙碌状态。
  没有可用托盘时，关闭窗口也走统一客户端退出流程。
  退出不取消服务预约、不停止服务；只等待本地显示请求和音频恢复收尾。
  服务任务仍忙碌不会单独阻止退出，退出后不再自动发起注销。
- 托盘注册由唯一 D-Bus owner 和代次共同标识，丢弃旧注册的迟到回调。
- USB4 物理断开会重置稳定性和自动绑定状态。PCI 逻辑移除仍保留路由抑制，
  直到真正拔线；同一 UUID 重插不能被旧抑制状态挡住。

## 配置恢复

`/var/lib/usb4-gpu-manager` 保存：

| 文件 | 含义 |
| --- | --- |
| `backups/<版本>/manifest.json` | 修改前的文件内容、权限和所有者 |
| `backups/<版本>/previous-backup.json` | 上一个可回滚版本 |
| `latest-backup.json` | 最近成功部署的回滚入口 |
| `deployment-pending.json` | 未完成事务的备份指针，首次部署失败时也存在 |
| `restart-required.json` | 本次配置修改的 boot ID，独立于被恢复或删除的 profile |

先写完整备份和恢复日志，再修改系统文件；配置、udev、systemd、
initramfs 与备份链全部成功后才删除恢复日志。恢复失败保留日志，
下次 `recover/rollback` 优先完成这笔事务。恢复成功仍要求当前启动周期重启；
不能用备份里的旧 `deployed_boot` 判断本次回滚是否已经生效。
卸载脚本同时检查未完成日志、重启标记和 profile。

## 驱动与实验边界

- 外接 GPU 的自动和手动连接都持有 logind sleep/shutdown inhibitor，
  稳定等待及模块加载、绑定前检查睡眠信号。
- 4070 与音频成对切换。modprobe 超时且子进程已退出时恢复原绑定；
  子进程仍阻塞时保留 override，不并发回绑，提示检查状态并重启。
- 安装脚本在复制前设置运行时 `RefuseManualStart`，停止现有服务，
  安装成功后移除保护。中途失败保留保护，重新完成安装即可解除；
  保护文件位于 `/run/systemd/system/usb4-gpu-manager.service.d/99-install-guard.conf`。
- Mutter 启动器拒绝私有构建时清理动态库、GI 和实验环境变量，
  使用系统数据目录。DRM-only 软光标按视图判断，覆盖外接和内置策略。
- 修改补丁生成器时同步更新分发 patch 和 overlay；保持 hunk 长度与新文件内容一致。

## 检视问题对应关系

| 问题 | 修复位置 |
| --- | --- |
| 拔线后自动绑定状态未复位 | `controller.tick` |
| 连接等待与睡眠竞争 | `core.operations.DeviceOperations._attach_locked`、`logind.sleep_inhibitor` |
| 队列最终检查遗漏新增显示器 | `core.operations.DeviceOperations._detach_locked` |
| 4070 modprobe 超时遗漏补偿 | `internal_gpu.switch_internal_gpu` |
| GUI 授权期间重入及完成回调互相覆盖 | `ui.window.Window.submit/apply_status` |
| 两个异步请求同时获得接受回执 | `controller.reserve_operation`、`daemon._perform/_job` |
| 无托盘关闭窗口绕过会话资源收尾 | `ui.window.Window.close_to_tray`、`ui.application.Application.request_quit` |
| 托盘消失后旧注册回调误报可用 | `tray.Tray.appeared/vanished` |
| 首次部署失败后找不到备份 | `deployment._begin_recovery/rollback` |
| 回滚旧配置丢失本次重启要求 | `deployment.status` |
| 首次回滚失败仍允许卸载 | `deployment.status`、`uninstall.sh` |
| 安装期间服务被 D-Bus 拉起 | `install.sh` |
| 实验启动回退残留私有库环境 | `launch_shell.system_environment` |
| 内置 DRM-only 混合视图软光标遗漏 | `drm_only_patch.py`、分发 patch |

新增与更新的回归用例位于 `tests/test_review_regressions.py`、
`test_internal_gpu.py`、`test_tray.py`、`test_poc_control.py` 和 `test_api.py`。
架构调整另补 `tests/test_architecture.py`，覆盖前端退出、会话收尾、
后台 RPC 线程、共享契约、BAR 判断、递归文件清单和 staging 内容/权限。
硬件方法迁移后，测试补丁指向 `core.operations`，避免补丁仅修改兼容入口而未生效。
本次工作区未受信任，未执行项目测试、构建或真机验证；
静态语法与补丁结构检查不能替代 Linux/真实 GPU 验收。

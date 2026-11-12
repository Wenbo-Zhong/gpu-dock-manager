# 内置 RTX 4070 的运行时休眠观察

## 本机实测

外置 RTX PRO 5000 已移除。GNOME Shell PID 3963158 继续运行，持有的 DRM 设备属于 AMD，内屏正常输出。内置 4070 的 DP 和 HDMI 均未接屏。GPU `0000:65:00.0` 仍绑定 `nvidia`，音频功能 `0000:65:00.1` 仍绑定 `vfio-pci`，两者同属 IOMMU 组 22。

两项 PCI 功能及上游桥均观察到 `runtime_status=suspended`、`power_state=D3cold`，GPU 和音频功能的 `runtime_usage=0`。NVIDIA 595.99.02 报告细粒度 Runtime D3 已启用。确认本机可以在保留 AMD 桌面的情况下让 4070 进入 D3cold，期间 GPU 驱动仍保持绑定。

另做了一轮约 33 秒的被动采样。每约 120 毫秒读取两项功能的 PCI 电源状态和管理服务子进程，不主动执行 NVIDIA 查询。

| 相对时间 | GPU 电源与运行时状态 | 同期观察 |
| --- | --- | --- |
| 0.00 秒 | D0，active | 采样开始 |
| 5.55 秒 | D3cold，suspended | 音频也进入 D3cold |
| 9.17 秒 | D3cold，suspended | 观察到管理服务的 `nvidia-smi` 子进程 |
| 9.77 秒 | D0，resuming | `nvidia-smi` 仍在运行 |
| 11.09 秒 | D0，active | 查询进程已退出 |
| 32.19 秒 | D3cold，suspended | 两项 PCI 功能再次进入 D3cold |

该窗口的内核累计计时增加约 27.42 秒 active 和 5.61 秒 suspended。已安装服务的代码确认默认每隔至少 10 秒执行一次未限定 PCI 地址的 NVIDIA 指标查询，即使仅有内置卡。采样捕捉到其中一次查询和 D3cold 唤醒的时序。尚未做关闭后台 NVIDIA 采样的对照，因此无法量化全部唤醒时间或实际功耗差。

Ptyxis 在前后采样时仍运行，并观察到它持有 4070 的 renderD129 和 NVIDIA 驱动节点。ROG Control Center 持有共享 `/dev/nvidia-uvm`。用户态扫描看不到部分系统进程的 fd，结果有覆盖限制。细粒度 RTD3 能在应用仍持有设备句柄的情况下休眠。驱动切换和 VFIO 隔离仍需单独判断进程占用、显示输出及音频功能。

## 对管理器的影响

- 设备页从 sysfs 独立显示 PCI 电源状态、运行时状态、驱动和连接器。展示 D3cold 时应避免用 NVIDIA 指标探测刷新功耗，无法读取的数值显示未知。
- 默认后台轮询应避开休眠的内置卡，完整 NVIDIA 指标可作为显式查询。两张卡同时存在时，仍需实测限定 BDF 的 NVML 查询是否会唤醒另一张卡。
- 驱动隔离单独研究。先在外屏无输出、GPU 无活动任务的条件下验证整组切换和失败回退，涵盖 `65:00.0` 及 `65:00.1`。外置卡的 PCI 移除和 USB4 流程继续独立。

## 11 月 10 日的对照准备

管理器现从 sysfs 读取 `power_state` 和 `power/runtime_status`，并在设备页分卡展示。后台 NVIDIA 指标查询只接受选中的外置 NVIDIA 显卡，传入该卡 PCI 地址，过滤其他卡的结果。外置目标消失时立即清除旧指标。只有内置 4070 的场景中，跟踪工作区版本的一次完整控制器刷新，未发现启动 `nvidia-smi`，读取到 4070 为 `D3cold/suspended`、AMD 为 `D0/active`。

更新后已核对正在运行的服务及安装文件。保持外置卡断开，做了 33.045 秒对照。274 个状态采样全部为 GPU 与音频功能同时 `D3cold/suspended`，两项功能各增加 33,045 毫秒休眠时间，运行时间增加 0。未观察到 `nvidia-smi` 子进程。此前窗口的 GPU 休眠时间为 5.612 秒，约占 17%，新版这一轮约占 100%。GNOME 仍是原进程，AMD 保持 D0，Ptyxis 和 ROG Control Center 也仍在运行。

这次比较的是 PCI 运行时状态。没有测量整机功耗。双卡都在时，仍需实测外置卡按 PCI 地址查询会不会唤醒 4070。命令限制返回的显卡，NVML 初始化对另一张卡的影响尚待实测。

资料：[NVIDIA 595.99.02 Runtime D3](https://download.nvidia.com/XFree86/Linux-x86_64/595.99.02/README/dynamicpowermanagement.html) · [Linux 运行时电源管理](https://docs.kernel.org/power/runtime_pm.html)

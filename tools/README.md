# TTY / GPU 释放验证 demo

从当前 Wayland 桌面的终端执行（无需重新安装服务）：

```bash
sudo /usr/bin/python3 -I /home/user/gpu-dock-manager/tools/vt_gpu_release_demo.py
```

记录当前图形会话和 GPU 占用 → 从 tty3–tty15 选择空闲 VT → 每两秒直接检查 `/proc/*/fd` → 默认 10 秒后返回原 VT。
排除 tty1/tty2、内核已分配的 VT 和 logind 已登记的会话，避免切到 GDM 登录界面。
不会注销、重启 GNOME/GDM、删除 PCI 设备、卸载驱动或结束 GPU 任务。
实验限定另一张 GPU 的内屏已启用，且目标 eGPU 没有连接外屏；已有管理操作或状态过期时退出。

独立子进程会在实验时间加 15 秒后尝试回切，以应对主进程异常退出。
正常完成时停止该子进程；主进程回切失败则保留。内核或显示驱动卡死时无法保证回切有效。
运行前显示手动返回命令，例如 `sudo chvt 2`（实际 VT 由程序检测）。实验期间保持 USB4 连接。

报告位于 `/var/tmp/egpu-vt-demo-*/report.json`，含切换前、TTY 中和返回后的 GPU 句柄；不收集命令行和环境变量。
后两次完整采样均无占用，只能作为后续卸载实验的依据，不代表已验证安全拔卡。
当前脚本**不会执行卸载**。

只读预检：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/vt_gpu_release_demo.py --check-only
```

普通用户可能无法完整读取其他用户进程的设备句柄；正式验证需要 sudo。
`--seconds 5` 至 `--seconds 30` 可调整 TTY 停留时间。

## 本机验证记录（2026-11-05）

GNOME 50.1 / Wayland / RTX PRO 5000 Blackwell：tty2 → tty3 → tty2，约 10 秒。
5 次 TTY 内采样均完整，PCI 设备持续存在，无提前退出或回切错误。
`gnome-shell` 始终持有目标卡的渲染节点、`/dev/nvidia0` 和 `/dev/nvidiactl`；
`Xwayland` 与 `ptyxis` 也持续持有 GPU 设备句柄。
切回原桌面成功，但单纯暂停会话并未释放这些图形资源，不能据此执行安全卸载。
该结论限于本机此次配置；没有进行 PCI 移除或驱动卸载实验。
# NVIDIA EGL 引用生命周期探针

`deferred_nvidia_probe.py --amd-pci 0000:66:00.0 --nvidia-pci 0000:03:00.0 --case amd-plus-nvidia-kms` 在 AMD 硬件 context 存活时，独立对照 NVIDIA DRM/GBM/EGL 访问。支持 `amd-only`、`amd-plus-nvidia-gbm`、`amd-plus-nvidia-egl`、`amd-plus-nvidia-dumb-buffer`。
dumb-buffer 用例只分配、映射并写入一个 64×64 私有缓冲，随后销毁，不向显示器提交；完整 EGL 用例会临时改变并显式恢复探针线程的当前 context，均不操作 GNOME。
实测与限制见 `docs/reports/2026-11-07-drm-only-feasibility.md`。

`gbm_lifetime_series.py --pci 0000:03:00.0 --cycles 3` 在一个子进程内重复完整上下文生命周期；加 `--fresh-thread` 则每轮新建并退出工作线程，用于区分线程退出与进程保留。整组子进程有 25 秒期限，不操作 GNOME。

`gbm_context_probe.py --pci 0000:03:00.0` 测试 GBM 平台显示生命周期，加 `--with-context` 则增加与 Mutter 硬件探测相似的临时 GLES2 上下文绑定和 GL_RENDERER 查询。
不创建窗口、surface 或模式设置，不改变 GNOME；GPU 会被初始化，可能唤醒。实测过程与限制见 `docs/reports/2026-11-07-gbm-context-lifetime.md`。

`egl_reference_probe.py --pci 0000:03:00.0` 以普通用户运行，在独立子进程中记录 EGL 加载、设备枚举、显示初始化、terminate 和 release-thread 前后的显卡 fd。
它会初始化 NVIDIA EGL，可能唤醒显卡，但不建立 context/surface，不操作当前 GNOME，不执行设备移除；不是安全拔卡检测。设备 PCI 地址应按当前机器调整。
结果保存到当前用户 `usb4-gpu-manager/poc-reports` 状态目录；本机实测及局限见 `docs/reports/2026-11-06-poc-reference-observations.md`。


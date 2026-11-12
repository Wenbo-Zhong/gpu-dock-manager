# 贡献

本项目主程序采用 GPL-3.0-only，随附 Mutter 补丁保留 GPL-2.0-or-later。
仓库：https://github.com/Wenbo-Zhong/gpu-dock-manager
提交的原创修改应采用对应文件的许可证。
使用系统 Python 与 GI 开发，提交前运行 README 中的检查命令。

自动测试不要调用真实 sysfs remove、modprobe、systemctl、udevadm 或 update-initramfs，
应使用临时文件系统和 mock。D-Bus 集成测试必须使用独立测试总线。

新增硬件支持时请说明发行版、内核、驱动、桌面、接口、接屏情况和以下结果：

- 启动、插入、拔出、重新连接，内置显示仍可用。
- 未接外屏、已接外屏、外屏禁用、未知状态的区分。
- CUDA 活跃、桌面持有 GPU、权限不足时拒绝移除。
- 挂起/恢复、部署失败恢复、回滚与离线恢复。

明确区分模拟测试与真实设备验证。新的发行版部署后端应独立实现，
不得通过猜测配置替换未知发行版的系统文件。

翻译贡献请阅读 [翻译指南](docs/translations.md)。界面文案位于独立的 gettext PO 文件，可直接提交新语言，无需修改 Python 代码。

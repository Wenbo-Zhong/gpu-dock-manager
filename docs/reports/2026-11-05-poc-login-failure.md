# 2026-11-05 Mutter PoC 图形登录故障

初版实验启动配置导致 GNOME 无法完成登录。用户在 TTY 执行 `session.py disable` 删除覆盖配置后恢复桌面。当前实际会话使用系统 `/usr/bin/gnome-shell`，无 PoC drop-in；系统 `libmutter-18-0` 文件校验无差异。

## 已确认的故障链

本机 journal 中 12:24:16 至 12:25:19 的五次启动均经过 AMD DRM/EGL 初始化，随后出现：

```text
cannot register existing type 'ClutterActor'
cannot register existing type 'ClutterAnimatable'
JS ERROR: Error: Unsupported type (null), deriving from fundamental (null)
Execution of main.js threw exception
org.gnome.Shell@ubuntu.service: Failed with result 'protocol'.
```

当时没有进入副卡回收操作。`protocol` 是 Shell 未完成服务启动的结果，不能据此归因于 systemd 启动器本身。

在不初始化 GPU 的独立进程里，以原生加载器打开私有 Mutter，再模拟 GI 的系统目录搜索，确认 Clutter/Cogl/Mtk 同时映射了私有和系统两份文件。各自的 `*_get_type` 符号地址不同。受控类型初始化也复现了同一个 `ClutterActor` 重复注册错误。

`LD_LIBRARY_PATH` 和 `ldd` 只验证了原生依赖，漏掉 GI 后续打开的系统路径。`gnome-shell --version` 不执行完整的 Shell JS 初始化，因此不能作为会话启动验证。

## 修复与保护

- 补丁只修改 Mutter 主库。私有目录的 Clutter/Cogl/Mtk SONAME 链接改为本机同版本系统文件，使原生代码与 GI 使用同一对象；未改动系统文件。
- `preflight.py` 在独立子进程中比较 Meta/Clutter/Cogl/Mtk 的原生与 GI 符号地址，核对 `/proc/self/maps`，不调用 GPU 初始化。构建、启用及启动器均检查；旧清单拒绝启用。
- `headless_smoke.py` 使用 bubblewrap 隐藏真实 `/dev` 设备、宿主总线，隔离 HOME、PID、网络及会话总线，用 Mesa 软件渲染启动完整 GNOME Shell JS。构建清单必须记录对应主库的启动验证。
- 单次试用在启动实验 Shell 前删除自有 drop-in、标记试用已消耗并刷新用户服务定义。撤销失败时启动系统版本；实验运行后失败，则后续登录恢复系统版本。保留 TTY `disable` 恢复入口。

隔离总线没有宿主服务激活目录，系统总线代理也指向这条私有总线。其自身的 AppArmor 查询关闭，宿主安全配置不变。系统服务缺失的日志仍会出现，故测试只能证明软件渲染环境中的 JS 启动及库一致性。

## 本次验证与边界

| 检查 | 结果 |
| --- | --- |
| 初版混载复现 | 失败符合预期，Clutter/Cogl/Mtk 均为两份映射 |
| 修复后的 GI/原生符号与库身份 | 四个命名空间全部一致 |
| 隔离 GNOME JS 启动 | 到达 `GNOME Shell started at`，继续存活至少 3 秒 |
| 隔离显示配置接口 | `GetCurrentState` 成功 |
| 运行中 GNOME 库映射 | 私有主库、系统三个 companion，各一份 |
| 实际 AMD Wayland 登录 | 修复后尚未重试，当前保留系统会话 |
| 实际 NVIDIA 引用释放 | 尚未执行，不能宣称已解决安全拔卡 |

私有 Mutter 主库未重新编译：此次修复改变运行时库连接方式，主库 SHA256 保持 `07482d1aac2080aeba25974b022f54b3b13d3712085567685772f92a82a2a962`。构建脚本同步加入同样的运行时配置与验证。

机器可读摘要见 [证据](evidence/2026-11-05-poc-login-failure.json)，验证状态见 [validation.json](../../experimental/mutter-poc/validation.json)。原始日志留在本机用户 cache，未将完整桌面日志加入项目。

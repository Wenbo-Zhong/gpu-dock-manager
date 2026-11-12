# Draft: device FDs retained after complete GBM/EGL cleanup on NVIDIA 595.99.02

Local draft only. No issue or forum post has been submitted. This is a question about resource lifetime, not a claim of a confirmed driver leak.

## Environment

- Ubuntu 26.04, NVIDIA open kernel module and userspace 595.99.02.
- USB4 RTX PRO 5000 72GB Blackwell; AMD GPU runs the Wayland desktop.
- Standalone ordinary-user Python ctypes reproducer, outside GNOME.
- No window, EGL surface, drawing, modeset, forced FD closure, or library unloading.

## Reproducer

`tools/gbm_context_probe.py --pci 0000:03:00.0 --with-context` performs:

1. Open target DRM card, create GBM device and GBM-platform EGLDisplay.
2. Initialize EGL, bind GLES, create a GLES2 no-config context.
3. Make it current with no surfaces and query GL_RENDERER.
4. Unbind, destroy context, terminate EGL, destroy GBM, close our card FD, release thread.
5. Observe process-local DRM/NVIDIA FDs immediately and one second later, then exit.

All boolean cleanup calls report success. Excluding our own card FD, device FD counts progress 0 → 6 at get-platform-display → 8 after initialize → 14 after create-context → 20 after make-current. Unbind, context destruction, terminate and GBM destruction do not reduce these counts. Release-thread reduces the count to 14.

A display-only control retains 8 device FDs. Three complete cycles in one thread retain 14 after each cycle, without observed growth. Three cycles on newly created threads also retain 14 after each thread has exited. Remaining distribution: four nvidiactl, seven nvidia0, two target render-node, one nvidia-modeset FDs. Every standalone child exits normally.

## Questions

- Is this process-lifetime retention expected? Which documented resource lifetime accounts for it?
- Is there a supported device-scoped shutdown operation that releases these references while keeping the process alive?
- Can such an operation preserve EGL contexts belonging to another GPU/vendor in the same Wayland compositor?
- What is the supported prerequisite sequence for logical eGPU release and subsequent reconnection on this driver branch?

We are not attempting to hot-unplug during the reproducer. The application is a GPU manager seeking a supported release sequence; FD absence alone would not be considered proof of safe removal.

Local evidence: `20261107-gbm-context-probe.json`, `20261107-gbm-thread-series.json` under this report directory's `evidence/`. Additional environment information should be collected and reviewed before submission.

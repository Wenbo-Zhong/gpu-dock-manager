"""固定数量的会话工作线程；远端 RPC 等待不延长 GUI 进程的退出。"""

from concurrent.futures import Future
from queue import Empty, Queue
import threading


class RequestPool:
    """后台线程不持有进程；会话资源收尾由 Application 显式等待。"""

    def __init__(self, max_workers=3):
        self.queue = Queue()
        self.lock = threading.Lock()
        self.closed = False
        self.threads = []
        for index in range(max_workers):
            # daemon=True 只控制客户端进程寿命；本地资源收尾由应用退出流程保证。
            thread = threading.Thread(target=self._run, name=f"gpu-session-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def submit(self, callback):
        with self.lock:
            # 与 shutdown 共用锁，避免关闭标记生效后又把新请求放入队列。
            if self.closed:
                raise RuntimeError("Session request pool is closed")
            future = Future()
            self.queue.put((future, callback))
            return future

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            future, callback = item
            # 尚未执行的请求可能已被退出流程取消，取消成功后不得再调用回调。
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(callback())
            except BaseException as error:
                future.set_exception(error)

    def shutdown(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            # 退出前本地变更已完成；尚未开始的只读刷新和远端请求不再发送。
            while True:
                try:
                    future, _ = self.queue.get_nowait()
                except Empty:
                    break
                future.cancel()
            # 每个线程消费一个结束标记；正在执行的远端 RPC 不在此同步等待。
            for _ in self.threads:
                self.queue.put(None)

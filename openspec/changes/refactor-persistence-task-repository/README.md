# refactor-persistence-task-repository

将任务队列、批次、反向图片 usage 和内部 callback 事实拆分到 scope-bound persistence repositories，并保留 `backend.database` 兼容导出。

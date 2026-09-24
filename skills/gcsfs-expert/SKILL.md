---
name: gcsfs-expert
description: Specialized domain engineering knowledge for Google Cloud Storage filesystem (gcsfs), fsspec async bridging, GCS REST/gRPC API contracts, concurrency preconditions, and event-loop safety. Use when modifying gcsfs code, diagnosing deadlocks/leaks, or reviewing gcsfs PRs.
---

# GCSFS Domain Expert Skill (`gcsfs-expert`)

This skill contains the domain rules, architecture invariants, and testing patterns for [`fsspec/gcsfs`](https://github.com/fsspec/gcsfs). Any code modifications, automated fixes, or reviews in `gcsfs` MUST adhere to the standards described below.

---

## 1. Architecture & Threading Model

`gcsfs` bridges synchronous Python file APIs (`open`, `read`, `write`, `ls`) with asynchronous I/O (`aiohttp`) using `fsspec.asyn.AsyncFileSystem`.

```text
[User Thread] (sync API: fs.ls(), file.read())
     │
     ▼ sync() helper
[fsspecIO Event Loop Thread] (async API: fs._ls(), session.request())
     │
     ▼ HTTP/REST
[Google Cloud Storage]
```

### Invariants:
1. **Never Call `sync()` Inside the Event Loop**:
   - Calling `sync(self.loop, ...)` or any synchronous wrapper from inside an `async def` method or from a thread running an event loop causes an **instant recursive deadlock**.
   - If you are already in an async context, call and `await` the private coroutine directly (e.g. `await self._cat_file(...)` instead of `self.cat_file(...)`).
2. **Never Run Blocking I/O Inside `async def`**:
   - All async operations in `gcsfs` share a single background loop thread (`_loop`).
   - Calling `time.sleep()`, synchronous `open()`, blocking socket calls, or `requests.*` halts the entire event loop, freezing all concurrent GCS operations across every thread in the application.
   - Always use `await asyncio.sleep()` or run blocking CPU operations via `loop.run_in_executor()`.

---

## 2. Resource Lifecycle & GC Safety

### Finalizers (`__del__`) Must Be Lock-Free
Python's garbage collector can trigger at any arbitrary point in any thread, including while holding internal locks or thread pool locks.
* **The Deadlock Trap**: In PR #1025, calling `ThreadPoolExecutor.submit()` inside `GCSFile.__del__` attempted to re-acquire Python's module-level non-reentrant `_global_shutdown_lock` during GC, freezing all I/O.
* **The Rule**: In `__del__` or object finalizers, **never acquire locks** and **never invoke thread pools**. Use lock-free data structures like `queue.SimpleQueue.put()` to enqueue background deferred cleanup.

### Prevent `aiohttp` Connection Leaks
* **Always Release Responses**:
  ```python
  # CORRECT:
  async with session.get(url) as resp:
      data = await resp.read()

  # OR if manual:
  resp = await session.get(url)
  try:
      ...
  finally:
      await resp.release()
  ```
* Never let a `ClientResponse` object be garbage collected without consuming or releasing the body; otherwise, TCP sockets linger in `CLOSE_WAIT` and exhaust connection pools.

### Retain References to Async Tasks
* When spawning background tasks with `loop.create_task()`, Python only keeps a weak reference to the task. If not referenced, the task can be garbage collected midway through execution, silently dropping exceptions.
* **Pattern**:
  ```python
  task = self.loop.create_task(coro)
  self._background_tasks.add(task)
  task.add_done_callback(self._background_tasks.discard)
  ```

---

## 3. GCS API Contracts & Invariants

### Resumable Upload Chunk Alignment
* The Google Cloud Storage JSON API requires chunk sizes for resumable uploads to be **multiples of 256 KiB (`262,144` bytes)**:
  `chunk_size % (256 * 1024) == 0`.
* Only the final chunk can be of arbitrary size. Violating this results in HTTP 400 invalid request errors.

### Generation & Precondition Checks (Prevent Clobbering)
* To prevent race conditions and lost updates when multiple clients write or delete the same object, always support and propagate:
  * `if_generation_match`: Object generation number (0 means object does not exist yet).
  * `if_metageneration_match`: Metadata generation counter.
* When retrying non-idempotent writes, preconditions are mandatory to avoid double-writes.

### Hierarchical Namespace (HNS) vs Flat Buckets
* In standard flat GCS buckets, directories are simulated via virtual prefixes and trailing slashes (`/`).
* In HNS-enabled buckets (Hierarchical Namespace), folders are true first-class entities.
* When checking if a path is a directory or removing directories, check bucket capabilities (`fs.is_hns_bucket(bucket)`) before issuing simulated prefix operations.

---

## 4. `fsspec` Return Contracts & Error Translation

### Standard Return Formats
* **`fs.ls(path, detail=False)`**: Must return a flat list of path strings (e.g. `['bucket/file1', 'bucket/file2']`).
* **`fs.ls(path, detail=True)`**: Must return a list of dictionaries with at least:
  * `name`: String full path without leading slash.
  * `size`: Integer size in bytes.
  * `type`: `"file"` or `"directory"`.
  * `generation`: String/int generation.

### Exception Mapping
Never leak raw HTTP client errors (`aiohttp.ClientResponseError`) to callers. Map them to standard Python / OS exceptions:
* `HTTP 404` (Not Found) $\rightarrow$ `FileNotFoundError(path)`
* `HTTP 403` (Forbidden) $\rightarrow$ `PermissionError(path)`
* `HTTP 412` (Precondition Failed) $\rightarrow$ `FileExistsError(path)` or `PreconditionFailed`
* Always preserve tracebacks using explicit chaining: `raise FileNotFoundError(path) from e`.

---

## 5. Testing & Verification

### Local GCS Emulator
* Use `fsouza/fake-gcs-server`:
  ```bash
  docker run -d -p 4443:4443 --name gcs_emulator fsouza/fake-gcs-server:latest -scheme http -public-host 0.0.0.0:4443
  ```
* Environment variable to direct tests to emulator:
  ```bash
  export STORAGE_EMULATOR_HOST="http://localhost:4443/storage/v1"
  ```

### Pytest Guidelines
* **Run single test file**:
  ```bash
  pytest gcsfs/tests/test_core.py -v
  ```
* **Always Autospec Mocks**:
  ```python
  # CORRECT:
  with mock.patch("gcsfs.core.GCSFileSystem._call", autospec=True):
      ...
  ```
  Un-autospecced mocks allow nonexistent methods and wrong arguments to pass silently.
* **Avoid Loops in Test Bodies**: Parametrize test inputs using `@pytest.mark.parametrize` rather than iterating inside a test function.

//! Linux shutdown joins kernel thread lifetimes, not Tokio's pre-exit callbacks.

use std::{
	io,
	os::fd::{AsRawFd, FromRawFd, OwnedFd},
	sync::{Arc, Mutex, OnceLock, TryLockError, atomic::{AtomicU8, Ordering}, mpsc},
	time::{Duration, Instant},
};

use napi_derive::napi;
use tokio::runtime::{Builder, Runtime};

const DEADLINE: Duration = Duration::from_secs(5);
static INSTALLED: Mutex<bool> = Mutex::new(false);
static SHUTDOWN: OnceLock<Shutdown> = OnceLock::new();

// Reserve 100 ms: 50 ms for worker result publication and 50 ms for caller
// wakeup/return. Only the caller wait is bounded. The napi stop step remains
// unbounded in the detached helper. This assumes ordinary OS scheduling,
// not a real-time kernel.
const WORK_MARGIN: Duration = Duration::from_millis(100);
const RETURN_MARGIN: Duration = Duration::from_millis(50);

struct Shutdown {
	state: Arc<ShutdownState>,
	request: mpsc::Sender<Instant>,
	helper_tid: libc::pid_t,
	tids: Arc<Mutex<Vec<libc::pid_t>>>,
}

#[derive(Default)]
struct ShutdownState {
	deadline: OnceLock<Instant>,
	result: OnceLock<Result<(), String>>,
	timeout: OnceLock<String>,
	phase: AtomicU8,
}

impl Shutdown {
	fn prepare(threads: Threads, stop: impl FnOnce() + Send + 'static) -> io::Result<Self> {
		let state = Arc::new(ShutdownState::default());
		let worker = Arc::clone(&state);
		let tids = Arc::clone(&threads.tids);
		let (request, requested) = mpsc::channel();
		let (ready, started) = mpsc::sync_channel(0);
		// Prepare the helper during installation. Thread creation cannot consume
		// shutdown's budget. A timed-out helper is detached until process exit.
		std::thread::Builder::new().name("omp-tokio-shutdown".into()).spawn(move || {
			// SAFETY: gettid has no arguments. Publish before accepting requests.
			let tid = unsafe { libc::syscall(libc::SYS_gettid) as libc::pid_t };
			if ready.send(tid).is_err() { return; }
			// Disconnection without a request must never invoke stop().
			let Ok(deadline) = requested.recv() else { return; };
			worker.phase.store(1, Ordering::Release);
			stop();
			let result = if Instant::now() >= deadline {
				Err("Tokio shutdown failed: napi runtime shutdown timed out".into())
			} else {
				worker.phase.store(2, Ordering::Release);
				wait_threads(threads, deadline)
			};
			let result = result.map_err(|error| format!("{error}; omp-tokio-shutdown tid {tid}"));
			let _ = worker.result.set(result);
		})?;
		let helper_tid = started.recv().map_err(io::Error::other)?;
		Ok(Self { state, request, helper_tid, tids })
	}

	fn shutdown(&self, start: Instant) -> Result<(), String> {
		let deadline = *self.state.deadline.get_or_init(|| {
			let deadline = start + DEADLINE - WORK_MARGIN;
			// OnceLock sends exactly one request, including concurrent callers.
			let _ = self.request.send(deadline);
			deadline
		});
		loop {
			// Timeout is terminal even if the detached worker later succeeds.
			if let Some(error) = self.state.timeout.get() { return Err(error.clone()); }
			if let Some(result) = self.state.result.get() { return result.clone(); }
			let remaining = (deadline + WORK_MARGIN - RETURN_MARGIN).saturating_duration_since(Instant::now());
			if remaining.is_zero() {
				let error = self.state.timeout.get_or_init(|| {
					let part = match self.state.phase.load(Ordering::Acquire) {
						0 => "shutdown helper startup",
						1 => "napi runtime shutdown",
						_ => "thread registry / thread waits",
					};
					let mut error = format!("Tokio shutdown failed: {part} timed out; omp-tokio-shutdown tid {}", self.helper_tid);
					match self.tids.try_lock() {
						Ok(tids) => {
							for &tid in tids.iter() {
								match proc_thread_exists(tid) {
									Ok(true) => error.push_str(&format!("; tid {tid} still alive")),
									Ok(false) => {},
									Err(probe) => error.push_str(&format!("; tid {tid} proc check failed: {probe}")),
								}
							}
						},
						Err(_) => error.push_str("; live tid snapshot unavailable at deadline"),
					}
					error
				});
				return Err(error.clone());
			}
			std::thread::sleep(remaining.min(Duration::from_millis(1)));
		}
	}
}

enum ThreadWait {
	Pidfd(OwnedFd),
	Proc,
	Exited,
}

#[cfg(test)]
static POLL_OVERRIDE: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(0);
#[cfg(test)]
static PROC_OVERRIDE: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(0);

#[cfg(test)]
fn probe_override(value: &std::sync::atomic::AtomicI32) -> Option<io::Result<bool>> {
	match value.load(Ordering::SeqCst) {
		0 => None,
		1 => Some(Ok(false)),
		2 => Some(Ok(true)),
		errno => Some(Err(io::Error::from_raw_os_error(errno))),
	}
}

struct Thread {
	tid: libc::pid_t,
	wait: io::Result<ThreadWait>,
}

#[derive(Default)]
struct Registry {
	started: usize,
	records: Vec<Thread>,
	errors: Vec<String>,
}

struct Threads {
	// Separate from registry so deadline diagnostics never wait for its lock.
	tids: Arc<Mutex<Vec<libc::pid_t>>>,
	registry: Arc<Mutex<Registry>>,
	// Disconnection proves no start callback can run later. Tokio retains the
	// callback through the handles moved into successfully spawned OS threads.
	starts_closed: mpsc::Receiver<()>,
}

fn poll_thread(fd: &OwnedFd, deadline: Instant) -> io::Result<bool> {
	#[cfg(test)]
	if let Some(result) = probe_override(&POLL_OVERRIDE) { return result; }
	loop {
		let mut pollfd = libc::pollfd { fd: fd.as_raw_fd(), events: libc::POLLIN, revents: 0 };
		let remaining = deadline.saturating_duration_since(Instant::now());
		let millis = remaining.as_millis().min(i32::MAX as u128) as i32;
		// SAFETY: pollfd points to one initialized descriptor for this call.
		let result = unsafe { libc::poll(&raw mut pollfd, 1, millis) };
		if result < 0 {
			let error = io::Error::last_os_error();
			if error.raw_os_error() == Some(libc::EINTR) {
				if Instant::now() >= deadline { return Ok(false); }
				continue;
			}
			return Err(error);
		}
		if pollfd.revents & libc::POLLIN != 0 { return Ok(true); }
		if pollfd.revents != 0 {
			return Err(io::Error::other(format!("unexpected poll events {}", pollfd.revents)));
		}
		return Ok(false);
	}
}

fn proc_thread_exists(tid: libc::pid_t) -> io::Result<bool> {
	#[cfg(test)]
	if let Some(result) = probe_override(&PROC_OVERRIDE) { return result; }
	match std::fs::metadata(format!("/proc/self/task/{tid}")) {
		Ok(_) => Ok(true),
		Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
		Err(error) => Err(error),
	}
}

fn wait_proc_thread(tid: libc::pid_t, deadline: Instant) -> io::Result<bool> {
	loop {
		if !proc_thread_exists(tid)? { return Ok(true); }
		let remaining = deadline.saturating_duration_since(Instant::now());
		if remaining.is_zero() { return Ok(false); }
		std::thread::sleep(remaining.min(Duration::from_millis(1)));
	}
}

impl Registry {
	fn prune(&mut self) {
		let errors = &mut self.errors;
		self.records.retain(|thread| {
			match &thread.wait {
				Ok(ThreadWait::Pidfd(fd)) => match poll_thread(fd, Instant::now()) {
					Ok(true) => {},
					Ok(false) => return true,
					Err(error) => {
						let message = format!("tid {} prune poll failed: {error}", thread.tid);
						if !errors.contains(&message) { errors.push(message); }
						return true;
					},
				},
				Ok(ThreadWait::Proc | ThreadWait::Exited) => {},
				Err(_) => return true,
			}
			match proc_thread_exists(thread.tid) {
				Ok(exists) => exists,
				Err(error) => {
					let message = format!("tid {} prune proc check failed: {error}", thread.tid);
					if !errors.contains(&message) { errors.push(message); }
					true
				},
			}
		});
	}
}

fn build_runtime() -> io::Result<(Runtime, Threads)> {
	build_tracked_runtime(Builder::new_multi_thread(), false)
}

fn build_tracked_runtime(mut builder: Builder, force_fallback: bool) -> io::Result<(Runtime, Threads)> {
	let registry = Arc::new(Mutex::new(Registry::default()));
	let tids = Arc::new(Mutex::new(Vec::new()));
	let started_tids = Arc::clone(&tids);
	let starts = Arc::clone(&registry);
	let stops = Arc::clone(&registry);
	let (send, starts_closed) = mpsc::channel();
	// Preserve napi's defaults and enable_all(). B1 exempts only Tokio's static
	// signal socketpair: signal::registry::globals_init, Tokio 1.53.1
	// src/runtime/driver.rs:149-157. Disabling IO would break native consumers.
	let runtime = builder
		.enable_all()
		.thread_name("tokio-rt-worker")
		.on_thread_start(move || {
			let _keep_sender = &send;
			let mut registry = starts.lock().unwrap();
			registry.started += 1;
			registry.prune();
			// SAFETY: gettid has no arguments. Open on this thread to prevent tid reuse.
			let tid = unsafe { libc::syscall(libc::SYS_gettid) as libc::pid_t };
			let opened = open_thread(tid, force_fallback);
			let wait = match opened {
				Ok(fd) => Ok(ThreadWait::Pidfd(fd)),
				Err(error) => match error.raw_os_error() {
					Some(libc::EINVAL) | Some(libc::ENOSYS) => Ok(ThreadWait::Proc),
					Some(libc::ESRCH) => Ok(ThreadWait::Exited),
					_ => Err(error),
				},
			};
			registry.records.push(Thread { tid, wait });
			let mut tids = started_tids.lock().unwrap();
			tids.clear();
			tids.extend(registry.records.iter().map(|thread| thread.tid));
		})
		.on_thread_stop(move || {
			// This thread is still alive. Only prune exits absent from /proc.
			stops.lock().unwrap().prune();
		})
		.build()?;
	Ok((runtime, Threads { registry, tids, starts_closed }))
}

fn open_thread(tid: libc::pid_t, _force_fallback: bool) -> io::Result<OwnedFd> {
	#[cfg(test)]
	if _force_fallback { return Err(io::Error::from_raw_os_error(libc::EINVAL)); }
	// PIDFD_THREAD is O_EXCL on Linux >= 6.9.
	let raw = unsafe { libc::syscall(libc::SYS_pidfd_open, tid, libc::O_EXCL) };
	if raw < 0 { return Err(io::Error::last_os_error()); }
	// SAFETY: syscall returned a new owned descriptor.
	Ok(unsafe { OwnedFd::from_raw_fd(raw as i32) })
}

fn wait_threads(threads: Threads, deadline: Instant) -> Result<(), String> {
	let mut errors = Vec::new();
	// A count snapshot alone misses successfully spawned threads that have not
	// entered on_thread_start. Wait for callback ownership to end first.
	if threads.starts_closed.recv_timeout(deadline.saturating_duration_since(Instant::now()))
		!= Err(mpsc::RecvTimeoutError::Disconnected) {
		errors.push("thread start callbacks still retained at deadline".to_owned());
	}
	let records = {
		let mut registry = loop {
			match threads.registry.try_lock() {
				Ok(registry) => break registry,
				Err(TryLockError::Poisoned(error)) => return Err(format!("Tokio shutdown failed: thread registry poisoned: {error}")),
				Err(TryLockError::WouldBlock) => {
					let remaining = deadline.saturating_duration_since(Instant::now());
					if remaining.is_zero() { return Err("Tokio shutdown failed: thread registry lock timed out".into()); }
					std::thread::sleep(remaining.min(Duration::from_millis(1)));
				},
			}
		};
		errors.append(&mut registry.errors);
		std::mem::take(&mut registry.records)
	};
	for thread in &records {
		let result = match &thread.wait {
			Ok(ThreadWait::Pidfd(fd)) => poll_thread(fd, deadline).and_then(|ready| {
				// pidfd readiness preceded /proc removal by 46-181 µs in r5 log
				// 7d325181. Wait for both using the same shutdown deadline.
				if ready { wait_proc_thread(thread.tid, deadline) } else { Ok(false) }
			}),
			Ok(ThreadWait::Proc | ThreadWait::Exited) => wait_proc_thread(thread.tid, deadline),
			Err(error) => {
				errors.push(format!("tid {} pidfd_open failed: {error}", thread.tid));
				continue;
			},
		};
		match result {
			Ok(true) => {},
			Ok(false) => errors.push(format!("tid {} still alive", thread.tid)),
			Err(error) => errors.push(format!("tid {} wait failed: {error}", thread.tid)),
		}
	}
	if errors.is_empty() && Instant::now() >= deadline {
		errors.push("thread waits timed out".into());
	}
	// OwnedFd closes every pidfd on success and failure.
	if errors.is_empty() { Ok(()) } else { Err(format!("Tokio shutdown failed: {}", errors.join(", "))) }
}

pub(crate) fn install() -> napi::Result<()> {
	let mut installed = INSTALLED.lock().map_err(|e| napi::Error::from_reason(e.to_string()))?;
	if *installed { return Ok(()); }
	let (runtime, threads) = build_runtime().map_err(|e| napi::Error::from_reason(format!("Tokio runtime build failed: {e}")))?;
	let shutdown = Shutdown::prepare(threads, napi::bindgen_prelude::shutdown_async_runtime)
		.map_err(|e| napi::Error::from_reason(format!("Tokio shutdown helper failed: {e}")))?;
	SHUTDOWN.set(shutdown).map_err(|_| napi::Error::from_reason("Tokio shutdown already installed"))?;
	napi::bindgen_prelude::create_custom_tokio_runtime(runtime);
	*installed = true;
	Ok(())
}

#[napi(js_name = "__ompShutdownTokioRuntime")]
pub fn shutdown_tokio_runtime() -> napi::Result<()> {
	let start = Instant::now();
	let Some(shutdown) = SHUTDOWN.get() else { return Ok(()); };
	shutdown.shutdown(start).map_err(napi::Error::from_reason)
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
	use super::*;
	use std::{collections::{BTreeMap, BTreeSet}, fs, io::Write, process::Command};

	fn isolated(name: &str, test: impl FnOnce()) {
		if std::env::var("OMP_SHUTDOWN_TEST").as_deref() == Ok(name) {
			test();
			return;
		}
		let output = Command::new(std::env::current_exe().unwrap())
			.args(["--exact", &format!("tokio_shutdown::tests::{name}"), "--nocapture"])
			.env("OMP_SHUTDOWN_TEST", name)
			.output().unwrap();
		io::stdout().write_all(&output.stdout).unwrap();
		io::stderr().write_all(&output.stderr).unwrap();
		assert!(output.status.success(), "child failed: {}", output.status);
	}

	fn measured_call<T>(call: impl FnOnce(Instant) -> T) -> T {
		let start = Instant::now();
		let result = call(start);
		eprintln!("omp_shutdown_wall_us={}", start.elapsed().as_micros());
		result
	}

	fn measured_shutdown(shutdown: &Shutdown) -> Result<(), String> {
		measured_call(|start| shutdown.shutdown(start))
	}

	fn shutdown(runtime: Runtime, threads: Threads) {
		let shutdown = Shutdown::prepare(threads, move || runtime.shutdown_background()).unwrap();
		measured_shutdown(&shutdown).unwrap();
	}

	fn workers() -> Vec<String> {
		fs::read_dir("/proc/self/task").unwrap().filter_map(|entry| {
			let path = entry.unwrap().path();
			match fs::read_to_string(path.join("comm")) {
				Ok(name) if name.trim() == "tokio-rt-worker" => Some(path.display().to_string()),
				Ok(_) => None,
				Err(error) if error.kind() == io::ErrorKind::NotFound => None,
				Err(error) => panic!("read thread name: {error}"),
			}
		}).collect()
	}

	fn fds() -> BTreeMap<String, String> {
		// Close the snapshot directory before reading targets. No directory fd
		// exemption may hide an unrelated descriptor left open by shutdown.
		let paths: Vec<_> = fs::read_dir("/proc/self/fd").unwrap()
			.map(|entry| entry.unwrap().path()).collect();
		paths.into_iter().filter_map(|path| {
			match fs::read_link(&path) {
				Ok(target) => Some((path.file_name().unwrap().to_string_lossy().into_owned(), target.to_string_lossy().into_owned())),
				Err(error) if error.kind() == io::ErrorKind::NotFound => None,
				Err(error) => panic!("read fd target: {error}"),
			}
		}).collect()
	}

	fn synthetic_threads(wait: ThreadWait) -> Threads {
		let (send, starts_closed) = mpsc::channel();
		drop(send);
		Threads {
			tids: Arc::new(Mutex::new(vec![123456])),
			registry: Arc::new(Mutex::new(Registry {
				records: vec![Thread { tid: 123456, wait: Ok(wait) }],
				..Registry::default()
			})),
			starts_closed,
		}
	}

	fn test_fd() -> OwnedFd {
		// A real owned fd lets tests observe retention and close independently
		// of the kernel's pidfd availability. Poll readiness comes from the seam.
		let raw = unsafe { libc::eventfd(0, libc::EFD_CLOEXEC) };
		assert!(raw >= 0);
		unsafe { OwnedFd::from_raw_fd(raw) }
	}

	#[test]
	fn shutdown_ready_present_retains_until_absent() {
		isolated("shutdown_ready_present_retains_until_absent", || {
			POLL_OVERRIDE.store(2, Ordering::SeqCst);
			PROC_OVERRIDE.store(2, Ordering::SeqCst);
			let fd = test_fd();
			let raw = fd.as_raw_fd();
			let threads = synthetic_threads(ThreadWait::Pidfd(fd));
			let mut registry = threads.registry.lock().unwrap();
			registry.prune();
			assert_eq!(registry.records.len(), 1, "ready but present task lost tracking");
			assert!(unsafe { libc::fcntl(raw, libc::F_GETFD) } >= 0, "retained pidfd closed");
			PROC_OVERRIDE.store(1, Ordering::SeqCst);
			registry.prune();
			assert!(registry.records.is_empty(), "absent task stayed tracked");
			assert_eq!(unsafe { libc::fcntl(raw, libc::F_GETFD) }, -1);
			assert_eq!(io::Error::last_os_error().raw_os_error(), Some(libc::EBADF));
		});
	}

	#[test]
	fn shutdown_presence_failure_is_sticky() {
		isolated("shutdown_presence_failure_is_sticky", || {
			POLL_OVERRIDE.store(2, Ordering::SeqCst);
			PROC_OVERRIDE.store(2, Ordering::SeqCst);
			let threads = synthetic_threads(ThreadWait::Pidfd(test_fd()));
			let shutdown = Shutdown::prepare(threads, || {}).unwrap();
			let start = Instant::now();
			let error = measured_shutdown(&shutdown).unwrap_err();
			assert!(start.elapsed() >= Duration::from_millis(4900));
			assert!(start.elapsed() <= DEADLINE);
			assert!(error.contains("tid 123456 still alive"), "{error}");
			assert!(error.contains(&format!("omp-tokio-shutdown tid {}", shutdown.helper_tid)), "{error}");
			PROC_OVERRIDE.store(1, Ordering::SeqCst);
			assert_eq!(measured_shutdown(&shutdown).unwrap_err(), error);
		});
	}

	#[test]
	fn shutdown_probe_errors_retain_and_fail_without_duplicates() {
		isolated("shutdown_probe_errors_retain_and_fail_without_duplicates", || {
			// Each row selects a distinct probe/record path. Errors must remain
			// failures even if a later probe would report task disappearance.
			for kind in 0..4 {
				let wait = match kind {
					0 | 1 => ThreadWait::Pidfd(test_fd()),
					2 => ThreadWait::Exited,
					_ => ThreadWait::Proc,
				};
				POLL_OVERRIDE.store(if kind == 0 { libc::EIO } else { 2 }, Ordering::SeqCst);
				PROC_OVERRIDE.store(if kind == 0 { 1 } else { libc::EACCES }, Ordering::SeqCst);
				let threads = synthetic_threads(wait);
				let message = {
					let mut registry = threads.registry.lock().unwrap();
					for _ in 0..3 { registry.prune(); }
					assert_eq!(registry.records.len(), 1, "probe error removed tracking, kind {kind}");
					assert_eq!(registry.errors.len(), 1, "duplicate prune errors, kind {kind}");
					registry.errors[0].clone()
				};
				let shutdown = Shutdown::prepare(threads, || {}).unwrap();
				let error = measured_shutdown(&shutdown).unwrap_err();
				assert_eq!(error.matches(&message).count(), 1, "{error}");
				assert!(error.contains("tid 123456 wait failed:"), "{error}");

				// Prune errors remain terminal when final probes recover.
				let wait = match kind {
					0 | 1 => ThreadWait::Pidfd(test_fd()),
					2 => ThreadWait::Exited,
					_ => ThreadWait::Proc,
				};
				let threads = synthetic_threads(wait);
				threads.registry.lock().unwrap().prune();
				POLL_OVERRIDE.store(2, Ordering::SeqCst);
				PROC_OVERRIDE.store(1, Ordering::SeqCst);
				let shutdown = Shutdown::prepare(threads, || {}).unwrap();
				assert!(measured_shutdown(&shutdown).unwrap_err().contains(&message));
			}
		});
	}

	#[test]
	fn shutdown_runtime_step_timeout_is_sticky() {
		isolated("shutdown_runtime_step_timeout_is_sticky", || {
			let (release, blocked) = mpsc::channel();
			let (finished, done) = mpsc::channel();
			let threads = synthetic_threads(ThreadWait::Exited);
			let shutdown = Shutdown::prepare(threads, move || {
				blocked.recv().unwrap();
				finished.send(()).unwrap();
			}).unwrap();
			let start = Instant::now();
			let error = measured_shutdown(&shutdown).unwrap_err();
			assert!(start.elapsed() >= Duration::from_millis(4900));
			assert!(start.elapsed() <= DEADLINE);
			assert!(error.contains("napi runtime shutdown timed out"), "{error}");
			release.send(()).unwrap();
			done.recv_timeout(DEADLINE).unwrap();
			assert_eq!(measured_shutdown(&shutdown).unwrap_err(), error);
		});
	}

	#[test]
	fn shutdown_timeout_names_live_tids_and_helper() {
		isolated("shutdown_timeout_names_live_tids_and_helper", || {
			let mut builder = Builder::new_multi_thread();
			builder.worker_threads(1);
			let (runtime, threads) = build_tracked_runtime(builder, false).unwrap();
			let (started, receive) = mpsc::channel();
			let (release_worker, blocked_worker) = mpsc::channel();
			runtime.spawn(async move {
				let tid = unsafe { libc::syscall(libc::SYS_gettid) as libc::pid_t };
				started.send(tid).unwrap();
				blocked_worker.recv().unwrap();
			});
			let tid = receive.recv_timeout(DEADLINE).unwrap();
			let (release_stop, blocked_stop) = mpsc::channel();
			let shutdown = Shutdown::prepare(threads, move || {
				blocked_stop.recv().unwrap();
				runtime.shutdown_background();
			}).unwrap();
			let start = Instant::now();
			let error = measured_shutdown(&shutdown).unwrap_err();
			let elapsed = start.elapsed();
			assert!(elapsed >= Duration::from_millis(4900));
			assert!(elapsed <= DEADLINE);
			assert!(error.contains(&format!("omp-tokio-shutdown tid {}", shutdown.helper_tid)), "{error}");
			assert!(error.contains(&format!("tid {tid} still alive")), "{error}");
			assert!(error.contains("napi runtime shutdown timed out"), "{error}");
			eprintln!("{error}");
			release_worker.send(()).unwrap();
			release_stop.send(()).unwrap();
			assert!(wait_proc_thread(shutdown.helper_tid, Instant::now() + DEADLINE).unwrap());
			assert_eq!(measured_shutdown(&shutdown).unwrap_err(), error);
		});
	}

	#[test]
	fn shutdown_helper_idle_no_wakeups() {
		isolated("shutdown_helper_idle_no_wakeups", || {
			let threads = synthetic_threads(ThreadWait::Exited);
			let shutdown = Shutdown::prepare(threads, || {}).unwrap();
			let tid = shutdown.helper_tid;
			let switches = || {
				let status = fs::read_to_string(format!("/proc/self/task/{tid}/status")).unwrap();
				let count = |key: &str| -> u64 {
					status.lines().find_map(|line| line.strip_prefix(key)).unwrap().trim().parse().unwrap()
				};
				(count("voluntary_ctxt_switches:"), count("nonvoluntary_ctxt_switches:"))
			};
			// Allow the startup rendezvous to finish before measuring the idle recv.
			std::thread::sleep(Duration::from_millis(50));
			let before = switches();
			eprintln!("omp_helper_idle_tid={tid}");
			std::thread::sleep(Duration::from_secs(10));
			let after = switches();
			let delta = (after.0 - before.0, after.1 - before.1);
			eprintln!("omp_helper_idle_ctxt_delta={},{}", delta.0, delta.1);
			assert_eq!(delta, (0, 0));
			assert_eq!(shutdown.shutdown(Instant::now()), Ok(()));
		});
	}

	#[test]
	fn shutdown_drop_without_request_skips_stop() {
		isolated("shutdown_drop_without_request_skips_stop", || {
			let (stopped, receive) = mpsc::channel();
			let shutdown = Shutdown::prepare(synthetic_threads(ThreadWait::Exited), move || {
				stopped.send(()).unwrap();
			}).unwrap();
			let tid = shutdown.helper_tid;
			drop(shutdown);
			assert_eq!(receive.recv_timeout(DEADLINE), Err(mpsc::RecvTimeoutError::Disconnected));
			assert!(wait_proc_thread(tid, Instant::now() + DEADLINE).unwrap());
		});
	}

	#[test]
	fn shutdown_registry_lock_is_bounded() {
		isolated("shutdown_registry_lock_is_bounded", || {
			let threads = synthetic_threads(ThreadWait::Exited);
			let registry = Arc::clone(&threads.registry);
			let held = registry.lock().unwrap();
			let shutdown = Shutdown::prepare(threads, || {}).unwrap();
			let start = Instant::now();
			let error = measured_shutdown(&shutdown).unwrap_err();
			assert!(start.elapsed() >= Duration::from_millis(4900));
			assert!(start.elapsed() <= DEADLINE);
			assert!(error.contains("thread registry lock timed out"), "{error}");
			drop(held);
			assert_eq!(measured_shutdown(&shutdown).unwrap_err(), error);
		});
	}

	#[test]
	fn shutdown_forced_pidfd_fallback() {
		isolated("shutdown_forced_pidfd_fallback", || {
			let (runtime, threads) = build_tracked_runtime(Builder::new_multi_thread(), true).unwrap();
			runtime.block_on(runtime.spawn(async {})).unwrap();
			{
				let registry = threads.registry.lock().unwrap();
				assert!(registry.started > 0);
				assert_eq!(registry.records.len(), registry.started);
				assert!(registry.records.iter().all(|thread| matches!(thread.wait, Ok(ThreadWait::Proc))),
					"forced EINVAL must record every started worker for /proc waiting");
			}
			shutdown(runtime, threads);
			assert!(workers().is_empty(), "fallback left Tokio workers: {:?}", workers());
		});
	}

	#[test]
	fn shutdown_blocking_churn_bounds_fds() {
		isolated("shutdown_blocking_churn_bounds_fds", || {
			let mut builder = Builder::new_multi_thread();
			builder.worker_threads(2).thread_keep_alive(Duration::from_millis(10));
			let (runtime, threads) = build_tracked_runtime(builder, false).unwrap();
			let mut bound = None;
			let mut previous_starts = 0;
			for _ in 0..12 {
				let tid = runtime.block_on(runtime.spawn_blocking(|| {
					unsafe { libc::syscall(libc::SYS_gettid) as libc::pid_t }
				})).unwrap();
				let wait = {
					let registry = threads.registry.lock().unwrap();
					registry.records.iter().find(|thread| thread.tid == tid).map(|thread| {
						match thread.wait.as_ref().unwrap() {
							ThreadWait::Pidfd(fd) => ThreadWait::Pidfd(fd.try_clone().unwrap()),
							ThreadWait::Proc => ThreadWait::Proc,
							ThreadWait::Exited => ThreadWait::Exited,
						}
					})
				};
				// Wait outside the registry lock so the stop hook can finish.
				// A missing record was already pruned after a confirmed exit.
				match wait {
					Some(ThreadWait::Pidfd(fd)) => {
						let deadline = Instant::now() + DEADLINE;
						assert!(poll_thread(&fd, deadline).unwrap());
						assert!(wait_proc_thread(tid, deadline).unwrap());
					},
					Some(ThreadWait::Proc | ThreadWait::Exited) => assert!(wait_proc_thread(tid, Instant::now() + DEADLINE).unwrap()),
					_ => {},
				}
				let registry = threads.registry.lock().unwrap();
				assert!(registry.started > previous_starts, "blocking thread did not churn");
				previous_starts = registry.started;
				let current = fds().len();
				// F3 bounds this 12-round workload, not arbitrary retention.
				let limit = *bound.get_or_insert(current + 2);
				assert!(current <= limit, "fd count grew across blocking churn: {current} > {limit}");
			}
			shutdown(runtime, threads);
			assert!(workers().is_empty());
		});
	}

	#[test]
	fn shutdown_workers_stop() {
		isolated("shutdown_workers_stop", || {
			let (runtime, threads) = build_runtime().unwrap();
			assert_eq!(runtime.block_on(runtime.spawn(async { 42 })).unwrap(), 42);
			shutdown(runtime, threads);
			assert!(workers().is_empty(), "Tokio workers remain: {:?}", workers());
		});
	}

	#[test]
	fn shutdown_closes_fds() {
		isolated("shutdown_closes_fds", || {
			let baseline = fds();
			let (runtime, threads) = build_runtime().unwrap();
			// B1: exactly Tokio's process-static signal socketpair is exempt.
			// signal::registry::globals_init, Tokio 1.53.1 src/runtime/driver.rs:149-157.
			// Record inode targets immediately after the first build, never fd numbers.
			let sockets: BTreeSet<String> = fds().into_iter()
				.filter(|(fd, target)| baseline.get(fd) != Some(target) && target.starts_with("socket:["))
				.map(|(_, target)| target).collect();
			assert_eq!(sockets.len(), 2, "expected exactly one signal socketpair: {sockets:?}");
			shutdown(runtime, threads);
			let check = || {
				let current = fds();
				let remaining: BTreeMap<_, _> = current.iter().filter(|(_, target)| !sockets.contains(*target))
					.map(|(fd, target)| (fd.clone(), target.clone())).collect();
				assert_eq!(remaining, baseline, "leftover fds and targets: {current:?}");
				let exempt: Vec<_> = current.values().filter(|target| sockets.contains(*target)).cloned().collect();
				assert_eq!(exempt.len(), 2, "signal socketpair grew or disappeared: {current:?}");
				assert_eq!(exempt.into_iter().collect::<BTreeSet<_>>(), sockets);
			};
			check();
			for _ in 0..20 {
				let (runtime, threads) = build_runtime().unwrap();
				runtime.block_on(runtime.spawn(async {})).unwrap();
				shutdown(runtime, threads);
				check();
			}
		});
	}

	#[test]
	fn shutdown_deadline_names_live_tid() {
		isolated("shutdown_deadline_names_live_tid", || {
			let (runtime, threads) = build_runtime().unwrap();
			let (started, receive) = mpsc::channel();
			let (release, blocked) = mpsc::channel();
			runtime.spawn(async move {
				let tid = unsafe { libc::syscall(libc::SYS_gettid) as libc::pid_t };
				started.send(tid).unwrap();
				blocked.recv().unwrap();
			});
			let tid = receive.recv_timeout(DEADLINE).unwrap();
			let shutdown = Shutdown::prepare(threads, move || runtime.shutdown_background()).unwrap();
			let start = Instant::now();
			let error = measured_shutdown(&shutdown).unwrap_err();
			assert!(error.contains(&format!("tid {tid} still alive")), "{error}");
			assert!(start.elapsed() >= Duration::from_millis(4900));
			assert!(start.elapsed() <= DEADLINE);
			release.send(()).unwrap();
		});
	}

	#[test]
	fn shutdown_settings_match() {
		isolated("shutdown_settings_match", || {
			let baseline = Builder::new_multi_thread().enable_all().build().unwrap();
			let expected = baseline.metrics().num_workers();
			drop(baseline);
			let (runtime, threads) = build_runtime().unwrap();
			assert_eq!(runtime.metrics().num_workers(), expected);
			let name = runtime.block_on(runtime.spawn(async { std::thread::current().name().unwrap().to_owned() })).unwrap();
			assert_eq!(name, "tokio-rt-worker");
			shutdown(runtime, threads);
		});
	}

	#[test]
	fn shutdown_second_call_is_noop() {
		isolated("shutdown_second_call_is_noop", || {
			install().unwrap();
			napi::bindgen_prelude::within_runtime_if_available(|| ());
			measured_call(|_| shutdown_tokio_runtime()).unwrap();
			measured_call(|_| shutdown_tokio_runtime()).unwrap();
			assert!(workers().is_empty());
			napi::bindgen_prelude::shutdown_async_runtime();
			assert!(workers().is_empty());
		});
	}
}

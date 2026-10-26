//! Linux shutdown joins kernel thread lifetimes, not Tokio's pre-exit callbacks.

use std::{
	io,
	os::fd::{AsRawFd, FromRawFd, OwnedFd},
	sync::{Arc, Mutex, mpsc},
	time::{Duration, Instant},
};

use napi_derive::napi;
use tokio::runtime::{Builder, Runtime};

const DEADLINE: Duration = Duration::from_secs(5);
static INSTALLED: Mutex<bool> = Mutex::new(false);
static THREADS: Mutex<Option<Threads>> = Mutex::new(None);

enum ThreadWait {
	Pidfd(OwnedFd),
	Proc,
	Exited,
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
	registry: Arc<Mutex<Registry>>,
	// Disconnection proves no start callback can run later. Tokio retains the
	// callback through the handles moved into successfully spawned OS threads.
	starts_closed: mpsc::Receiver<()>,
}

fn poll_thread(fd: &OwnedFd, deadline: Instant) -> io::Result<bool> {
	loop {
		let mut pollfd = libc::pollfd { fd: fd.as_raw_fd(), events: libc::POLLIN, revents: 0 };
		let remaining = deadline.saturating_duration_since(Instant::now());
		let millis = remaining.as_nanos().div_ceil(1_000_000).min(i32::MAX as u128) as i32;
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
		self.records.retain(|thread| match &thread.wait {
			Ok(ThreadWait::Pidfd(fd)) => match poll_thread(fd, Instant::now()) {
				Ok(exited) => !exited,
				Err(error) => {
					errors.push(format!("tid {} prune poll failed: {error}", thread.tid));
					true
				},
			},
			Ok(ThreadWait::Proc) => proc_thread_exists(thread.tid).unwrap_or(true),
			Ok(ThreadWait::Exited) => false,
			Err(_) => true,
		});
	}
}

fn build_runtime() -> io::Result<(Runtime, Threads)> {
	build_tracked_runtime(Builder::new_multi_thread(), false)
}

fn build_tracked_runtime(mut builder: Builder, force_fallback: bool) -> io::Result<(Runtime, Threads)> {
	let registry = Arc::new(Mutex::new(Registry::default()));
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
		})
		.on_thread_stop(move || {
			// This thread is still alive. Only prune kernel-confirmed exits.
			stops.lock().unwrap().prune();
		})
		.build()?;
	Ok((runtime, Threads { registry, starts_closed }))
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
		let mut registry = threads.registry.lock().unwrap();
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
			Ok(ThreadWait::Proc) => wait_proc_thread(thread.tid, deadline),
			Ok(ThreadWait::Exited) => Ok(true),
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
	// OwnedFd closes every pidfd on success and failure.
	if errors.is_empty() { Ok(()) } else { Err(format!("Tokio shutdown failed: {}", errors.join(", "))) }
}

pub(crate) fn install() -> napi::Result<()> {
	let mut installed = INSTALLED.lock().map_err(|e| napi::Error::from_reason(e.to_string()))?;
	if *installed { return Ok(()); }
	let (runtime, threads) = build_runtime().map_err(|e| napi::Error::from_reason(format!("Tokio runtime build failed: {e}")))?;
	*THREADS.lock().map_err(|e| napi::Error::from_reason(e.to_string()))? = Some(threads);
	napi::bindgen_prelude::create_custom_tokio_runtime(runtime);
	*installed = true;
	Ok(())
}

#[napi(js_name = "__ompShutdownTokioRuntime")]
pub fn shutdown_tokio_runtime() -> napi::Result<()> {
	let deadline = Instant::now() + DEADLINE;
	let mut threads = THREADS.lock().map_err(|e| napi::Error::from_reason(e.to_string()))?;
	let Some(threads) = threads.take() else { return Ok(()); };
	napi::bindgen_prelude::shutdown_async_runtime();
	wait_threads(threads, deadline).map_err(napi::Error::from_reason)
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
	use super::*;
	use std::{collections::{BTreeMap, BTreeSet}, fs, process::Command};

	fn isolated(name: &str, test: impl FnOnce()) {
		if std::env::var("OMP_SHUTDOWN_TEST").as_deref() == Ok(name) {
			test();
			return;
		}
		let output = Command::new(std::env::current_exe().unwrap())
			.args(["--exact", &format!("tokio_shutdown::tests::{name}"), "--nocapture"])
			.env("OMP_SHUTDOWN_TEST", name)
			.output().unwrap();
		assert!(output.status.success(), "child failed: {}\n{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
	}

	fn shutdown(runtime: Runtime, threads: Threads) {
		let deadline = Instant::now() + DEADLINE;
		runtime.shutdown_background();
		wait_threads(threads, deadline).unwrap();
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
					Some(ThreadWait::Pidfd(fd)) => assert!(poll_thread(&fd, Instant::now() + DEADLINE).unwrap()),
					Some(ThreadWait::Proc) => assert!(wait_proc_thread(tid, Instant::now() + DEADLINE).unwrap()),
					_ => {},
				}
				let registry = threads.registry.lock().unwrap();
				assert!(registry.started > previous_starts, "blocking thread did not churn");
				previous_starts = registry.started;
				let current = fds().len();
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
			let start = Instant::now();
			runtime.shutdown_background();
			let error = wait_threads(threads, start + DEADLINE).unwrap_err();
			assert!(error.contains(&format!("tid {tid} still alive")), "{error}");
			assert!(start.elapsed() >= Duration::from_millis(4900));
			assert!(start.elapsed() < Duration::from_secs(6));
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
			shutdown_tokio_runtime().unwrap();
			shutdown_tokio_runtime().unwrap();
			assert!(workers().is_empty());
			napi::bindgen_prelude::shutdown_async_runtime();
			assert!(workers().is_empty());
		});
	}
}

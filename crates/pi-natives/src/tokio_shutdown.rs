//! Linux shutdown joins kernel thread lifetimes, not Tokio's pre-exit callbacks.

use std::{
	io,
	os::fd::{AsRawFd, FromRawFd, OwnedFd},
	sync::{Arc, Mutex, atomic::{AtomicUsize, Ordering}, mpsc},
	time::{Duration, Instant},
};

use napi_derive::napi;
use tokio::runtime::{Builder, Runtime};

const DEADLINE: Duration = Duration::from_secs(5);
static INSTALLED: Mutex<bool> = Mutex::new(false);
static THREADS: Mutex<Option<Threads>> = Mutex::new(None);

struct Thread {
	tid: libc::pid_t,
	fd: io::Result<Option<OwnedFd>>,
}

struct Threads {
	spawned: Arc<AtomicUsize>,
	started: mpsc::Receiver<Thread>,
}

fn build_runtime() -> io::Result<(Runtime, Threads)> {
	let spawned = Arc::new(AtomicUsize::new(0));
	let count = Arc::clone(&spawned);
	let (send, started) = mpsc::channel();
	// Preserve napi's defaults and enable_all(). B1 exempts only Tokio's static
	// signal socketpair: signal::registry::globals_init, Tokio 1.53.1
	// src/runtime/driver.rs:149-157. Disabling IO would break native consumers.
	let runtime = Builder::new_multi_thread()
		.enable_all()
		.thread_name_fn(move || {
			// Count before spawn so shutdown cannot miss a thread whose start hook
			// has not run yet. Keep Tokio's default name, including blocking threads.
			count.fetch_add(1, Ordering::SeqCst);
			"tokio-rt-worker".to_owned()
		})
		.on_thread_start(move || {
			// SAFETY: gettid has no arguments. Open on this thread to prevent tid reuse.
			let tid = unsafe { libc::syscall(libc::SYS_gettid) as libc::pid_t };
			// PIDFD_THREAD is O_EXCL on Linux >= 6.9.
			let raw = unsafe { libc::syscall(libc::SYS_pidfd_open, tid, libc::O_EXCL) };
			let fd = if raw >= 0 {
				// SAFETY: syscall returned a new owned descriptor.
				Ok(Some(unsafe { OwnedFd::from_raw_fd(raw as i32) }))
			} else {
				let error = io::Error::last_os_error();
				if error.raw_os_error() == Some(libc::ESRCH) { Ok(None) } else { Err(error) }
			};
			// A receiver dropped after timeout closes late descriptors here too.
			let _ = send.send(Thread { tid, fd });
		})
		.build()?;
	Ok((runtime, Threads { spawned, started }))
}

fn wait_threads(threads: Threads, deadline: Instant) -> Result<(), String> {
	let mut records = Vec::new();
	let mut errors = Vec::new();
	// shutdown_background closes the pool before this snapshot. No new spawn
	// can succeed afterwards. Receive each start once, within the same deadline.
	let expected = threads.spawned.load(Ordering::SeqCst);
	for _ in 0..expected {
		match threads.started.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
			Ok(thread) => records.push(thread),
			Err(error) => {
				errors.push(format!("{} thread starts unaccounted for: {error}", expected - records.len()));
				break;
			},
		}
	}
	for thread in &records {
		match &thread.fd {
			Ok(Some(fd)) => {
				let mut pollfd = libc::pollfd { fd: fd.as_raw_fd(), events: libc::POLLIN, revents: 0 };
				let remaining = deadline.saturating_duration_since(Instant::now());
				let millis = remaining.as_nanos().div_ceil(1_000_000).min(i32::MAX as u128) as i32;
				// SAFETY: pollfd points to one initialized descriptor for this call.
				let result = unsafe { libc::poll(&raw mut pollfd, 1, millis) };
				if result < 0 {
					errors.push(format!("tid {} poll failed: {}", thread.tid, io::Error::last_os_error()));
				} else if pollfd.revents & libc::POLLIN == 0 {
					errors.push(format!("tid {} still alive (poll events {})", thread.tid, pollfd.revents));
				}
			},
			Ok(None) => {}, // ESRCH: already exited.
			Err(error) => errors.push(format!("tid {} pidfd_open failed: {error}", thread.tid)),
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

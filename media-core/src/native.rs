//! Linux device backend. No model SDK or provider credentials are used here.
use crate::{validate_format, wire::*, PacketOrder};
use anyhow::{anyhow, bail, Context, Result};
use futures_core::Stream;
use gst::prelude::*;
use gstreamer as gst;
use gstreamer_app::{AppSink, AppSrc};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{BufRead, BufReader},
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    pin::Pin,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tokio::sync::{broadcast, mpsc};
use tokio_stream::{
    wrappers::{ReceiverStream, UnixListenerStream},
    StreamExt,
};
use tonic::{Request, Response, Status};

fn err(e: impl std::fmt::Display) -> Status {
    Status::failed_precondition(e.to_string())
}
type RpcStream<T> = Pin<Box<dyn Stream<Item = Result<T, Status>> + Send>>;
const AUDIO_SIZE: usize = 1920;
const VIDEO_SIZE: usize = 1280 * 720 * 2;

struct Devices {
    children: Vec<Child>,
    env: HashMap<String, String>,
}
impl Devices {
    fn start(config: &MediaStart) -> Result<Self> {
        if !cfg!(target_os = "linux") {
            bail!("Native devices require Linux");
        }
        let mut d = Self {
            children: vec![],
            env: HashMap::new(),
        };
        let root = PathBuf::from(&config.root);
        fs::create_dir_all(root.join("logs"))?;
        // The X server atomically selects a free display; no fixed display names.
        let mut xvfb = Command::new("Xvfb")
            .args([
                "-displayfd",
                "1",
                "-screen",
                "0",
                "1280x720x24",
                "-nolisten",
                "tcp",
                "-ac",
            ])
            .stdout(Stdio::piped())
            .stderr(fs::File::create(root.join("logs/display.log"))?)
            .spawn()?;
        let stdout = xvfb.stdout.take().unwrap();
        d.children.push(xvfb);
        let mut line = String::new();
        BufReader::new(stdout).read_line(&mut line)?;
        let display: u32 = line
            .trim()
            .parse()
            .context("Xvfb did not report a display")?;
        d.env.insert("DISPLAY".into(), format!(":{display}"));
        d.children.push(
            Command::new("openbox")
                .envs(&d.env)
                .stdout(Stdio::null())
                .stderr(fs::File::create(root.join("logs/window-manager.log"))?)
                .spawn()?,
        );
        if config.audio {
            let runtime = root.join("pulse-runtime");
            fs::create_dir_all(&runtime)?;
            fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700))?;
            d.env
                .insert("PULSE_RUNTIME_PATH".into(), runtime.display().to_string());
            let socket = root.join("pulse.sock");
            let server = format!("unix:{}", socket.display());
            let module = format!(
                "--load=module-native-protocol-unix socket={} auth-anonymous=1",
                socket.display()
            );
            d.children.push(
                Command::new("pulseaudio")
                    .envs(&d.env)
                    .args([
                        "-n",
                        "--daemonize=no",
                        "--exit-idle-time=-1",
                        "--disable-shm",
                        &module,
                    ])
                    .stdout(Stdio::null())
                    .stderr(fs::File::create(root.join("logs/audio.log"))?)
                    .spawn()?,
            );
            let until = Instant::now() + Duration::from_secs(8);
            while !socket.exists() {
                if Instant::now() > until {
                    bail!("PulseAudio startup timed out");
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            d.env.insert("PULSE_SERVER".into(), server.clone());
            d.env.insert("PULSE_SOURCE".into(), "mba_mic".into());
            d.env.insert("PULSE_SINK".into(), "mba_out".into());
            for args in [
                vec![
                    "load-module",
                    "module-null-sink",
                    "sink_name=mba_in",
                    "rate=48000",
                    "channels=1",
                ],
                vec![
                    "load-module",
                    "module-remap-source",
                    "master=mba_in.monitor",
                    "source_name=mba_mic",
                    "channels=1",
                    "source_properties=device.description=QA_MIC",
                ],
                vec![
                    "load-module",
                    "module-null-sink",
                    "sink_name=mba_out",
                    "rate=48000",
                    "channels=2",
                ],
                vec!["set-default-source", "mba_mic"],
                vec!["set-default-sink", "mba_out"],
            ] {
                let result = Command::new("pactl")
                    .arg(format!("--server={server}"))
                    .args(args)
                    .output()?;
                if !result.status.success() {
                    bail!("pactl: {}", String::from_utf8_lossy(&result.stderr));
                }
            }
        }
        Ok(d)
    }
}
impl Drop for Devices {
    fn drop(&mut self) {
        for child in self.children.iter_mut().rev() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

struct Pipeline(gst::Pipeline);
impl Drop for Pipeline {
    fn drop(&mut self) {
        let _ = self.0.set_state(gst::State::Null);
    }
}
fn pipeline(desc: &str) -> Result<Pipeline> {
    Ok(Pipeline(
        gst::parse::launch(desc)?
            .downcast::<gst::Pipeline>()
            .map_err(|_| anyhow!("expected pipeline"))?,
    ))
}
fn playing(p: &Pipeline) -> Result<()> {
    p.0.set_state(gst::State::Playing)?;
    Ok(())
}
fn check_pipeline(p: &Pipeline) -> Result<()> {
    if let Some(msg) = p.0.bus().unwrap().pop_filtered(&[gst::MessageType::Error]) {
        if let gst::MessageView::Error(e) = msg.view() {
            bail!("{}: {:?}", e.error(), e.debug());
        }
    }
    Ok(())
}
fn normal_format(kind: &str) -> MediaFormat {
    if kind == "audio" {
        MediaFormat {
            kind: kind.into(),
            encoding: "S16LE".into(),
            rate: 48000,
            channels: 1,
            ..Default::default()
        }
    } else {
        MediaFormat {
            kind: kind.into(),
            encoding: "YUY2".into(),
            width: 1280,
            height: 720,
            fps: 30,
            ..Default::default()
        }
    }
}
fn caps(f: &MediaFormat) -> Result<gst::Caps> {
    validate_format(f).map_err(|e| anyhow!(e))?;
    Ok(if f.kind == "audio" {
        gst::Caps::builder("audio/x-raw")
            .field("format", "S16LE")
            .field("layout", "interleaved")
            .field("rate", f.rate as i32)
            .field("channels", f.channels as i32)
            .build()
    } else {
        gst::Caps::builder("video/x-raw")
            .field("format", if f.encoding == "RGB24" { "RGB" } else { "YUY2" })
            .field("width", f.width as i32)
            .field("height", f.height as i32)
            .field("framerate", gst::Fraction::new(f.fps as i32, 1))
            .build()
    })
}
struct TrackState {
    pipeline: Pipeline,
    sink: AppSink,
    input: Option<AppSrc>,
    spec: Track,
    canceled: AtomicBool,
    input_claimed: AtomicBool,
    play_claimed: AtomicBool,
    first: Mutex<Option<Vec<u8>>>,
}
impl TrackState {
    fn create(spec: Track) -> Result<Self> {
        let tail = if spec.kind == "audio" {
            "audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved"
        } else {
            "videoconvert ! videoscale add-borders=true ! videorate ! video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1,pixel-aspect-ratio=1/1"
        };
        let prefix = if spec.path.is_empty() {
            "appsrc name=source format=time block=true max-bytes=5529600"
        } else {
            "uridecodebin name=source"
        };
        let p = pipeline(&format!(
            "{prefix} ! {tail} ! appsink name=out sync=false max-buffers=3 drop=false"
        ))?;
        let element = p.0.by_name("source").unwrap();
        let input = if spec.path.is_empty() {
            let source = element.downcast::<AppSrc>().unwrap();
            let f = spec
                .format
                .as_ref()
                .ok_or_else(|| anyhow!("stream format missing"))?;
            let size = validate_format(f).map_err(|e| anyhow!(e))?;
            source.set_max_bytes((size * if f.kind == "audio" { 10 } else { 3 }) as u64);
            if f.kind == "video" {
                source.set_property("block", false);
                source.set_property_from_str("leaky-type", "downstream");
            }
            source.set_caps(Some(&caps(
                spec.format
                    .as_ref()
                    .ok_or_else(|| anyhow!("stream format missing"))?,
            )?));
            Some(source)
        } else {
            let uri = gst::glib::filename_to_uri(&spec.path, None)?;
            element.set_property("uri", uri.as_str());
            None
        };
        let sink = p.0.by_name("out").unwrap().downcast::<AppSink>().unwrap();
        playing(&p)?;
        let state = Self {
            pipeline: p,
            sink,
            input,
            spec,
            canceled: AtomicBool::new(false),
            input_claimed: AtomicBool::new(false),
            play_claimed: AtomicBool::new(false),
            first: Mutex::new(None),
        };
        if state.input.is_none() {
            let deadline = Instant::now() + Duration::from_secs(15);
            loop {
                if let Some(data) = state.pull()? {
                    *state.first.lock().unwrap() = Some(data);
                    break;
                }
                if Instant::now() > deadline {
                    bail!("asset decode timeout");
                }
            }
        }
        Ok(state)
    }
    fn pull(&self) -> Result<Option<Vec<u8>>> {
        if self.canceled.load(Ordering::SeqCst) {
            bail!("action canceled");
        }
        if let Some(sample) = self
            .sink
            .try_pull_sample(gst::ClockTime::from_mseconds(100))
        {
            return Ok(Some(
                sample
                    .buffer()
                    .ok_or_else(|| anyhow!("sample missing buffer"))?
                    .map_readable()?
                    .as_slice()
                    .to_vec(),
            ));
        }
        check_pipeline(&self.pipeline)?;
        if self.sink.is_eos() {
            return Ok(Some(vec![]));
        }
        Ok(None)
    }
    fn cancel(&self) {
        self.canceled.store(true, Ordering::SeqCst);
        let _ = self.pipeline.0.set_state(gst::State::Null);
    }
}
struct Playback {
    data: Vec<u8>,
    track: Arc<TrackState>,
    ack: std::sync::mpsc::SyncSender<u64>,
}
struct Output {
    pipeline: Pipeline,
    sender: std::sync::mpsc::SyncSender<Playback>,
}
struct Engine {
    epoch: Instant,
    stopped: AtomicBool,
    error: Mutex<Option<String>>,
    devices: Mutex<Option<Devices>>,
    outputs: Mutex<HashMap<String, Arc<Output>>>,
    tracks: Mutex<HashMap<String, Arc<TrackState>>>,
    captures: Mutex<Vec<Pipeline>>,
    recordings: Mutex<Vec<Pipeline>>,
    recording: AtomicBool,
    recording_origin: AtomicU64,
    recording_end: AtomicU64,
    events: broadcast::Sender<Observation>,
    audio: broadcast::Sender<MediaPacket>,
    video: broadcast::Sender<MediaPacket>,
    sequence: AtomicU64,
    config: MediaStart,
    wav: Mutex<HashMap<String, std::sync::mpsc::SyncSender<Option<(Vec<u8>, u64)>>>>,
    wav_threads: Mutex<Vec<std::thread::JoinHandle<()>>>,
    display_frames: AtomicU64,
    display_gaps: AtomicU64,
    canceled: Mutex<HashSet<String>>,
    fragments: Mutex<HashMap<String, u64>>,
}
impl Engine {
    fn now(&self) -> u64 {
        self.epoch.elapsed().as_micros() as u64
    }
    fn emit(&self, kind: &str, data: serde_json::Value, at: u64) {
        let _ = self.events.send(Observation {
            r#type: kind.into(),
            json: data.to_string(),
            observed_at_us: at,
            sequence: self.sequence.fetch_add(1, Ordering::SeqCst) + 1,
        });
    }
    fn fail(&self, e: impl std::fmt::Display) {
        *self.error.lock().unwrap() = Some(e.to_string());
        self.emit(
            "media.error",
            serde_json::json!({"reason":e.to_string()}),
            self.now(),
        );
    }
    fn start(
        config: MediaStart,
        epoch: Instant,
        events: broadcast::Sender<Observation>,
    ) -> Result<Arc<Self>> {
        gst::init()?;
        let devices = Devices::start(&config)?;
        let (audio, _) = broadcast::channel(50);
        let (video, _) = broadcast::channel(3);
        let e = Arc::new(Self {
            epoch,
            stopped: AtomicBool::new(false),
            error: Mutex::new(None),
            devices: Mutex::new(Some(devices)),
            outputs: Mutex::new(HashMap::new()),
            tracks: Mutex::new(HashMap::new()),
            captures: Mutex::new(vec![]),
            recordings: Mutex::new(vec![]),
            recording: AtomicBool::new(false),
            recording_origin: AtomicU64::new(0),
            recording_end: AtomicU64::new(0),
            events,
            audio,
            video,
            sequence: AtomicU64::new(0),
            wav: Mutex::new(HashMap::new()),
            wav_threads: Mutex::new(vec![]),
            display_frames: AtomicU64::new(0),
            display_gaps: AtomicU64::new(0),
            canceled: Mutex::new(HashSet::new()),
            fragments: Mutex::new(HashMap::new()),
            config,
        });
        let result = (|| -> Result<()> {
            if e.config.audio {
                e.start_output("audio")?;
                e.start_capture("audio")?;
            }
            if e.config.camera {
                e.start_output("video")?;
            }
            if e.config.visual {
                e.start_capture("video")?;
            }
            if e.config.recording {
                e.record(true)?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            e.stop();
            return Err(error);
        }
        Ok(e)
    }
    fn environment(&self) -> HashMap<String, String> {
        self.devices
            .lock()
            .unwrap()
            .as_ref()
            .map(|d| d.env.clone())
            .unwrap_or_default()
    }
    fn start_output(self: &Arc<Self>, kind: &str) -> Result<()> {
        let desc = if kind == "audio" {
            "appsrc name=input is-live=true format=time block=false max-bytes=19200 ! audioconvert ! pulsesink name=device sync=true buffer-time=40000 latency-time=10000"
        } else {
            "appsrc name=input is-live=true format=time block=false max-bytes=5529600 ! v4l2sink name=device sync=true"
        };
        let p = pipeline(desc)?;
        let src = p.0.by_name("input").unwrap().downcast::<AppSrc>().unwrap();
        src.set_caps(Some(&caps(&normal_format(kind))?));
        let device = p.0.by_name("device").unwrap();
        if kind == "audio" {
            device.set_property("server", self.environment().get("PULSE_SERVER").unwrap());
            device.set_property("device", "mba_in");
        } else {
            device.set_property("device", &self.config.camera_device);
        }
        playing(&p)?;
        let (tx, rx) =
            std::sync::mpsc::sync_channel::<Playback>(if kind == "audio" { 10 } else { 3 });
        let output = Arc::new(Output {
            pipeline: p,
            sender: tx,
        });
        self.outputs
            .lock()
            .unwrap()
            .insert(kind.into(), output.clone());
        let readiness = output.clone();
        let e = self.clone();
        let kind = kind.to_string();
        std::thread::spawn(move || {
            let duration = if kind == "audio" {
                20_000
            } else {
                1_000_000 / 30
            };
            let idle = if kind == "audio" {
                vec![0; AUDIO_SIZE]
            } else {
                [16, 128, 16, 128].repeat(VIDEO_SIZE / 4)
            };
            let mut due = Instant::now();
            let max_bytes = if kind == "audio" { 19200 } else { 5529600 };
            while !e.stopped.load(Ordering::SeqCst) {
                if let Some(delay) = due.checked_duration_since(Instant::now()) {
                    std::thread::sleep(delay);
                }
                due += Duration::from_micros(duration);
                // A newly connected PulseAudio sink can take time to uncork. Respect
                // appsrc's bound before dequeuing, instead of overflowing on idle data.
                let wait_started = Instant::now();
                while src.current_level_bytes() >= max_bytes {
                    if e.stopped.load(Ordering::SeqCst) {
                        return;
                    }
                    if wait_started.elapsed() > Duration::from_secs(3) {
                        e.fail("device stalled while waiting for buffer capacity");
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(5));
                }
                let item = rx.try_recv().ok();
                let valid = item
                    .as_ref()
                    .filter(|i| !i.track.canceled.load(Ordering::SeqCst));
                let data = valid
                    .map(|i| i.data.clone())
                    .unwrap_or_else(|| idle.clone());
                if kind == "audio" {
                    e.record_pcm("injected", data.clone(), e.now());
                }
                let mut b = gst::Buffer::from_mut_slice(data);
                let pts = output
                    .pipeline
                    .0
                    .current_running_time()
                    .unwrap_or(gst::ClockTime::ZERO);
                b.get_mut().unwrap().set_pts(pts);
                b.get_mut()
                    .unwrap()
                    .set_duration(gst::ClockTime::from_useconds(duration));
                if let Err(error) = src.push_buffer(b) {
                    if !e.stopped.load(Ordering::SeqCst) {
                        e.fail(error);
                    }
                    break;
                }
                if src.current_level_bytes() > max_bytes {
                    e.fail("device buffer exceeded");
                    break;
                }
                if let Some(i) = valid {
                    let _ = i.ack.send(e.now());
                }
                if due + Duration::from_micros(duration * 2) < Instant::now() {
                    e.emit(
                        "injection.gap",
                        serde_json::json!({"stream":kind,"reason":"feeder_late"}),
                        e.now(),
                    );
                    due = Instant::now();
                }
            }
        });
        let (result, state, _) = readiness.pipeline.0.state(gst::ClockTime::from_seconds(3));
        result?;
        if state != gst::State::Playing {
            bail!("device did not become ready");
        }
        check_pipeline(&readiness.pipeline)?;
        Ok(())
    }
    fn capture_description(&self, kind: &str, tail: &str) -> String {
        if kind == "audio" {
            format!("pulsesrc name=source ! audioconvert ! audioresample ! audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved ! {tail}")
        } else {
            // Linking probes ximagesrc caps, which opens the display even in NULL state.
            // Set the session display before parse/link, not only before PLAYING.
            let env = self.environment();
            let display = env.get("DISPLAY").expect("session display");
            format!("ximagesrc name=source display-name={display} use-damage=false show-pointer=true ! video/x-raw,framerate=30/1 ! videoconvert ! video/x-raw,format=RGB ! {tail}")
        }
    }
    fn configure_capture(&self, p: &Pipeline, kind: &str) {
        let source = p.0.by_name("source").unwrap();
        let env = self.environment();
        if kind == "audio" {
            source.set_property("server", env.get("PULSE_SERVER").unwrap());
            source.set_property("device", "mba_out.monitor");
        } else {
            source.set_property("display-name", env.get("DISPLAY").unwrap());
        }
    }
    fn start_capture(self: &Arc<Self>, kind: &str) -> Result<()> {
        let p = pipeline(
            &self.capture_description(kind, "appsink name=out sync=false max-buffers=3 drop=false"),
        )?;
        self.configure_capture(&p, kind);
        let sink = p.0.by_name("out").unwrap().downcast::<AppSink>().unwrap();
        let weak = Arc::downgrade(self);
        let pipeline_weak = p.0.downgrade();
        let kind = kind.to_string();
        let mut seq = 0;
        let mut pending = Vec::new();
        let mut pending_at = 0;
        let mut last_video = None;
        sink.set_callbacks(gstreamer_app::AppSinkCallbacks::builder().new_sample(move|sink| {
   let sample=sink.pull_sample().map_err(|_|gst::FlowError::Eos)?;
   let Some(e)=weak.upgrade() else {return Err(gst::FlowError::Eos)};
   let buffer=sample.buffer().ok_or(gst::FlowError::Error)?;let mapped=buffer.map_readable().map_err(|_|gst::FlowError::Error)?;
   let running=pipeline_weak.upgrade().and_then(|p|p.current_running_time()).map(|v|v.useconds()).unwrap_or(0);
   let at=e.now().saturating_sub(running.saturating_sub(buffer.pts().map(|v|v.useconds()).unwrap_or(running)));
   let mut format=normal_format(&kind);
   if kind=="audio" {
    e.record_pcm("speaker",mapped.as_slice().to_vec(),at);
    format.channels=2;if pending.is_empty() {pending_at=at;} pending.extend_from_slice(mapped.as_slice());
    while pending.len()>=3840 {
     let data:Vec<_>=pending.drain(..3840).collect();let _=e.audio.send(MediaPacket{stream_id:"speaker".into(),sequence:seq,pts_us:pending_at,data,format:Some(format.clone()),..Default::default()});seq+=1;pending_at+=20_000;
    }
   } else {
    let structure=sample.caps().unwrap().structure(0).unwrap();format.encoding="RGB24".into();format.width=structure.get::<i32>("width").unwrap() as u32;format.height=structure.get::<i32>("height").unwrap() as u32;
    e.display_frames.fetch_add(1,Ordering::Relaxed);
    if let Some(last)=last_video {if at>last+50_000 {e.display_gaps.fetch_add((at-last)/33_333-1,Ordering::Relaxed);e.emit("capture.gap",serde_json::json!({"stream":"display","reason":"timestamp_gap","start_us":last,"end_us":at}),at);}} last_video=Some(at);
    e.record_frame(mapped.as_slice(),at);
    let _=e.video.send(MediaPacket{stream_id:"display".into(),sequence:seq,pts_us:at,data:mapped.as_slice().to_vec(),format:Some(format),..Default::default()});seq+=1;
   }
   Ok(gst::FlowSuccess::Ok)
  }).build());
        playing(&p)?;
        self.captures.lock().unwrap().push(p);
        Ok(())
    }
    fn check(&self) -> Result<()> {
        if let Some(error) = self.error.lock().unwrap().as_ref() {
            bail!("{error}");
        }
        for p in self.outputs.lock().unwrap().values() {
            check_pipeline(&p.pipeline)?;
        }
        for p in self.captures.lock().unwrap().iter() {
            check_pipeline(p)?;
        }
        for p in self.recordings.lock().unwrap().iter() {
            self.publish_fragments(p);
        }
        if let Some(devices) = self.devices.lock().unwrap().as_mut() {
            for c in &mut devices.children {
                if c.try_wait()?.is_some() {
                    bail!("device process exited");
                }
            }
        }
        Ok(())
    }
    fn play(self: &Arc<Self>, track: Arc<TrackState>, start: u64) -> Result<()> {
        if track.play_claimed.swap(true, Ordering::SeqCst) {
            bail!("track already played");
        }
        let output = self
            .outputs
            .lock()
            .unwrap()
            .get(&track.spec.kind)
            .cloned()
            .ok_or_else(|| anyhow!("modality disabled"))?;
        let size = if track.spec.kind == "audio" {
            AUDIO_SIZE
        } else {
            VIDEO_SIZE
        };
        let step = if track.spec.kind == "audio" {
            20_000
        } else {
            1_000_000 / 30
        };
        let mut bytes = track.first.lock().unwrap().take().unwrap_or_default();
        let mut eos = false;
        let mut index = 0;
        loop {
            if self.stopped.load(Ordering::SeqCst) || track.canceled.load(Ordering::SeqCst) {
                bail!("action canceled");
            }
            while bytes.len() < size && !eos {
                if let Some(data) = track.pull()? {
                    if data.is_empty() {
                        eos = true;
                    } else {
                        bytes.extend(data);
                    }
                }
            }
            if bytes.is_empty() && eos {
                break;
            }
            if eos && bytes.len() < size {
                bytes.resize(size, 0);
            }
            let due = start + index * step;
            while self.now() < due {
                if track.canceled.load(Ordering::SeqCst) {
                    bail!("action canceled");
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            let data = bytes.drain(..size).collect();
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            output.sender.send(Playback {
                data,
                track: track.clone(),
                ack: tx,
            })?;
            let at = loop {
                match rx.recv_timeout(Duration::from_millis(100)) {
                    Ok(at) => break at,
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                        self.check()?;
                        if track.canceled.load(Ordering::SeqCst) {
                            bail!("action canceled");
                        }
                    }
                    Err(e) => return Err(e.into()),
                }
            };
            if index == 0 {
                self.emit("injection.started",serde_json::json!({"action_id":track.spec.action_id,"stream":track.spec.kind,"boundary":"appsrc","actual_us":at,"requested_us":start}),at);
            }
            index += 1;
        }
        std::thread::sleep(Duration::from_millis(60));
        self.emit("injection.completed",serde_json::json!({"action_id":track.spec.action_id,"boundary":"local_sink_drain_estimate","uncertainty_us":60000}),self.now());
        Ok(())
    }
    fn cancel(&self, id: &str) {
        self.canceled.lock().unwrap().insert(id.into());
        for t in self
            .tracks
            .lock()
            .unwrap()
            .values()
            .filter(|t| t.spec.action_id == id)
        {
            t.cancel();
        }
    }
    fn record_pcm(&self, name: &str, data: Vec<u8>, at: u64) {
        // The combined video receives the same samples as the independent WAV evidence.
        // All recorder inputs use one session-relative origin, preserving offsets.
        let origin = self.recording_origin.load(Ordering::SeqCst);
        let channels = if name == "speaker" { 2 } else { 1 };
        let skip = ((origin.saturating_sub(at) * 48000 / 1_000_000) as usize * channels * 2)
            .min(data.len());
        if skip < data.len() {
            for p in self.recordings.lock().unwrap().iter() {
                if let Some(src) = p.0.by_name(name).and_then(|e| e.downcast::<AppSrc>().ok()) {
                    let duration =
                        (data.len() - skip) as u64 * 1_000_000 / (48000 * channels as u64 * 2);
                    let mut buffer = gst::Buffer::from_mut_slice(data[skip..].to_vec());
                    let b = buffer.get_mut().unwrap();
                    b.set_pts(gst::ClockTime::from_useconds(at.saturating_sub(origin)));
                    b.set_duration(gst::ClockTime::from_useconds(duration));
                    if src.push_buffer(buffer).is_err() || src.current_level_bytes() > 48000 * 4 * 2
                    {
                        self.fail("combined audio recorder stalled");
                    }
                    self.recording_end
                        .fetch_max(at.max(origin) + duration, Ordering::SeqCst);
                }
            }
        }
        if let Some(tx) = self.wav.lock().unwrap().get(name) {
            if tx.try_send(Some((data, at))).is_err() {
                self.fail("audio recording queue overflow");
            }
        }
    }
    fn start_wav(self: &Arc<Self>, name: &str, channels: u16) -> Result<()> {
        let (tx, rx) = std::sync::mpsc::sync_channel::<Option<(Vec<u8>, u64)>>(200);
        self.wav.lock().unwrap().insert(name.into(), tx);
        let weak = Arc::downgrade(self);
        let name = name.to_string();
        let root = PathBuf::from(&self.config.root).join("recordings");
        let epoch = self.now();
        let handle = std::thread::spawn(move || {
            let result = (|| -> Result<()> {
                let mut writer: Option<hound::WavWriter<std::io::BufWriter<fs::File>>> = None;
                let mut path = PathBuf::new();
                let mut count = 0u64;
                let mut index = 0;
                let mut start = 0;
                let publish = |path: &PathBuf, start: u64, count: u64| {
                    if let Some(e) = weak.upgrade() {
                        e.emit("audio.segment.available",serde_json::json!({"path":path,"stream":name,"start_us":start,"end_us":start+count*1_000_000/(48000*channels as u64),"complete":true}),start);
                    }
                };
                while let Ok(Some((data, at))) = rx.recv() {
                    for (i, chunk) in data.chunks_exact(2).enumerate() {
                        if writer.is_none() {
                            path = root.join(format!("{name}-{epoch}-{index:05}.wav"));
                            start = at + i as u64 * 1_000_000 / (48000 * channels as u64);
                            writer = Some(hound::WavWriter::create(
                                &path,
                                hound::WavSpec {
                                    channels,
                                    sample_rate: 48000,
                                    bits_per_sample: 16,
                                    sample_format: hound::SampleFormat::Int,
                                },
                            )?);
                            count = 0;
                            index += 1;
                        }
                        writer
                            .as_mut()
                            .unwrap()
                            .write_sample(i16::from_le_bytes([chunk[0], chunk[1]]))?;
                        count += 1;
                        if count == 48000 * channels as u64 * 10 {
                            writer.take().unwrap().finalize()?;
                            publish(&path, start, count);
                        }
                    }
                }
                if let Some(writer) = writer {
                    writer.finalize()?;
                    publish(&path, start, count);
                }
                Ok(())
            })();
            if let Err(error) = result {
                if let Some(e) = weak.upgrade() {
                    e.fail(error);
                }
            }
        });
        self.wav_threads.lock().unwrap().push(handle);
        Ok(())
    }
    fn record_frame(&self, data: &[u8], at: u64) {
        for p in self.recordings.lock().unwrap().iter() {
            if let Some(src) =
                p.0.by_name("input")
                    .and_then(|e| e.downcast::<AppSrc>().ok())
            {
                let mut b = gst::Buffer::from_mut_slice(data.to_vec());
                let origin = self.recording_origin.load(Ordering::SeqCst);
                b.get_mut()
                    .unwrap()
                    .set_pts(gst::ClockTime::from_useconds(at.saturating_sub(origin)));
                b.get_mut()
                    .unwrap()
                    .set_duration(gst::ClockTime::from_useconds(33_333));
                if src.push_buffer(b).is_err() || src.current_level_bytes() > 1280 * 720 * 3 * 30 {
                    self.fail("video recorder stalled");
                }
                self.recording_end
                    .fetch_max(at.max(origin) + 33_333, Ordering::SeqCst);
            }
        }
    }
    fn record(self: &Arc<Self>, enabled: bool) -> Result<()> {
        if enabled == self.recording.load(Ordering::SeqCst) {
            return Ok(());
        }
        if !enabled {
            self.finish_recordings()?;
            self.recording.store(false, Ordering::SeqCst);
            return Ok(());
        }
        let root = PathBuf::from(&self.config.root).join("recordings");
        fs::create_dir_all(&root)?;
        if self.config.audio {
            self.start_wav("speaker", 2)?;
            self.start_wav("injected", 1)?;
        }
        if self.config.visual {
            // Encode video once, then retain both recoverable segments and one playable file.
            let mut description = String::from(
                "appsrc name=input is-live=true format=time block=false max-bytes=82944000 caps=video/x-raw,format=RGB,width=1280,height=720,framerate=30/1 \
                 ! videoconvert ! vp8enc deadline=1 cpu-used=8 keyframe-max-dist=30 ! tee name=encoded \
                 encoded. ! queue ! splitmuxsink name=record muxer-factory=webmmux max-size-time=10000000000 \
                 encoded. ! queue ! full_mux.video_0 \
                 webmmux name=full_mux ! filesink name=full_file"
            );
            if self.config.audio {
                // Recorder audio is timestamp-driven, not clock-dropped. Half-gain per
                // input avoids clipping during overlap; original WAV tracks are unchanged.
                description.push_str(
                    " appsrc name=speaker format=time block=false max-bytes=384000 caps=audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved \
                      ! queue ! volume volume=0.5 ! mix. \
                      appsrc name=injected format=time block=false max-bytes=192000 caps=audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved \
                      ! queue ! volume volume=0.5 ! mix. \
                      audiomixer name=mix ! audioconvert ! audio/x-raw,rate=48000,channels=2 \
                      ! opusenc bitrate=96000 ! queue ! full_mux.audio_0"
                );
            }
            let p = pipeline(&description)?;
            let origin = self.now();
            self.recording_origin.store(origin, Ordering::SeqCst);
            self.recording_end.store(origin, Ordering::SeqCst);
            let location = root.join(format!("display-{origin}-%05d.webm"));
            p.0.by_name("record")
                .unwrap()
                .set_property("location", location.to_str().unwrap());
            p.0.by_name("full_file").unwrap().set_property(
                "location",
                root.join(format!("session-{origin}.webm"))
                    .to_str()
                    .unwrap(),
            );
            playing(&p)?;
            self.recordings.lock().unwrap().push(p);
        }
        self.recording.store(true, Ordering::SeqCst);
        Ok(())
    }
    fn fragment_message(&self, p: &Pipeline, msg: &gst::MessageRef) {
        if let gst::MessageView::Element(e) = msg.view() {
            if let Some(s) = e.structure() {
                if s.name().starts_with("splitmuxsink-fragment-") {
                    if let Ok(path) = s.get::<String>("location") {
                        let running = s
                            .get::<gst::ClockTime>("running-time")
                            .map(|t| t.useconds())
                            .unwrap_or(0);
                        let current =
                            p.0.current_running_time()
                                .map(|t| t.useconds())
                                .unwrap_or(running);
                        let at = self.now().saturating_sub(current.saturating_sub(running));
                        if s.name().ends_with("opened") {
                            self.fragments.lock().unwrap().insert(path, at);
                        } else if s.name().ends_with("closed") {
                            let start = self.fragments.lock().unwrap().remove(&path).unwrap_or(at);
                            self.emit("recording.segment.available",serde_json::json!({"path":path,"stream":"display","start_us":start,"end_us":at,"complete":true}),at);
                        }
                    }
                }
            }
        }
    }
    fn publish_fragments(&self, p: &Pipeline) {
        let bus = p.0.bus().unwrap();
        while let Some(msg) = bus.pop() {
            match msg.view() {
                gst::MessageView::Error(e) => self.fail(e.error()),
                _ => self.fragment_message(p, &msg),
            }
        }
    }
    fn finish_recordings(&self) -> Result<()> {
        self.wav.lock().unwrap().clear();
        for thread in self.wav_threads.lock().unwrap().drain(..) {
            let _ = thread.join();
        }
        let mut recordings = self.recordings.lock().unwrap();
        for p in recordings.iter() {
            for name in ["input", "speaker", "injected"] {
                if let Some(src) = p.0.by_name(name).and_then(|e| e.downcast::<AppSrc>().ok()) {
                    let _ = src.end_of_stream();
                }
            }
        }
        let mut failed = false;
        for p in recordings.iter() {
            let deadline = Instant::now() + Duration::from_secs(4);
            let mut ended = false;
            while Instant::now() < deadline {
                if let Some(msg) =
                    p.0.bus()
                        .unwrap()
                        .timed_pop(gst::ClockTime::from_mseconds(100))
                {
                    match msg.view() {
                        gst::MessageView::Eos(..) => {
                            ended = true;
                            break;
                        }
                        gst::MessageView::Error(e) => {
                            self.fail(e.error());
                            break;
                        }
                        _ => self.fragment_message(p, &msg),
                    }
                }
            }
            failed |= !ended;
            if ended {
                // Close the file before publishing it for clients to download.
                p.0.set_state(gst::State::Null)?;
                let path: String = p.0.by_name("full_file").unwrap().property("location");
                let start = self.recording_origin.load(Ordering::SeqCst);
                let end = self.recording_end.load(Ordering::SeqCst);
                if end > start {
                    self.emit("recording.available", serde_json::json!({
                        "path": path, "stream": "session", "start_us": start, "end_us": end,
                        "complete": true,
                        "audio_streams": if self.config.audio { vec!["speaker", "injected"] } else { vec![] }
                    }), end);
                }
            }
        }
        recordings.clear();
        if failed {
            bail!("recording finalization failed");
        }
        Ok(())
    }
    fn stop(&self) {
        self.emit("capture.metrics",serde_json::json!({"display_frames":self.display_frames.load(Ordering::Relaxed),"display_gap_frames":self.display_gaps.load(Ordering::Relaxed)}),self.now());
        self.stopped.store(true, Ordering::SeqCst);
        for t in self.tracks.lock().unwrap().values() {
            t.cancel();
        }
        if let Err(e) = self.finish_recordings() {
            self.fail(e);
        }
        self.captures.lock().unwrap().clear();
        for o in self.outputs.lock().unwrap().values() {
            let _ = o.pipeline.0.set_state(gst::State::Null);
        }
        self.outputs.lock().unwrap().clear();
        self.tracks.lock().unwrap().clear();
        self.devices.lock().unwrap().take();
        self.emit("media.stopped", serde_json::json!({}), self.now());
    }
}

#[derive(Clone)]
struct Service {
    engine: Arc<Mutex<Option<Arc<Engine>>>>,
    epoch: Instant,
    events: broadcast::Sender<Observation>,
}
impl Service {
    fn engine(&self) -> Result<Arc<Engine>, Status> {
        self.engine
            .lock()
            .unwrap()
            .clone()
            .ok_or_else(|| err("media not started"))
    }
}
#[tonic::async_trait]
impl media_server::Media for Service {
    async fn start(&self, req: Request<MediaStart>) -> Result<Response<Ready>, Status> {
        let config = req.into_inner();
        if config.protocol_version != 1 {
            return Err(err("incompatible protocol version"));
        }
        if self.engine.lock().unwrap().is_some() {
            return Err(err("already started"));
        }
        let epoch = self.epoch;
        let events = self.events.clone();
        let e = tokio::task::spawn_blocking(move || Engine::start(config, epoch, events))
            .await
            .map_err(err)?
            .map_err(err)?;
        let env = e.environment();
        *self.engine.lock().unwrap() = Some(e);
        Ok(Response::new(Ready {
            protocol_version: 1,
            now_us: self.epoch.elapsed().as_micros() as u64,
            environment: env,
            capabilities: vec!["streaming".into()],
        }))
    }
    async fn health(&self, _: Request<Empty>) -> Result<Response<Ready>, Status> {
        let e = self.engine()?;
        e.check().map_err(err)?;
        for p in e.recordings.lock().unwrap().iter() {
            e.publish_fragments(p);
        }
        Ok(Response::new(Ready {
            protocol_version: 1,
            now_us: e.now(),
            environment: e.environment(),
            capabilities: vec![],
        }))
    }
    async fn prepare(&self, req: Request<Track>) -> Result<Response<Empty>, Status> {
        let e = self.engine()?;
        let spec = req.into_inner();
        let key = format!("{}:{}", spec.action_id, spec.kind);
        if !e.outputs.lock().unwrap().contains_key(&spec.kind) {
            return Err(err("modality disabled"));
        }
        if e.tracks.lock().unwrap().contains_key(&key) {
            return Err(err("duplicate track"));
        }
        let t = tokio::task::spawn_blocking(move || TrackState::create(spec))
            .await
            .map_err(err)?
            .map_err(err)?;
        let canceled = e.canceled.lock().unwrap();
        if e.stopped.load(Ordering::SeqCst) || canceled.contains(&t.spec.action_id) {
            t.cancel();
            return Err(err("action already canceled"));
        }
        e.tracks.lock().unwrap().insert(key, Arc::new(t));
        Ok(Response::new(Empty {}))
    }
    async fn play(&self, req: Request<Track>) -> Result<Response<Empty>, Status> {
        let e = self.engine()?;
        let spec = req.into_inner();
        let t = e
            .tracks
            .lock()
            .unwrap()
            .get(&format!("{}:{}", spec.action_id, spec.kind))
            .cloned()
            .ok_or_else(|| err("unknown track"))?;
        tokio::task::spawn_blocking(move || e.play(t, spec.start_us))
            .await
            .map_err(err)?
            .map_err(err)?;
        Ok(Response::new(Empty {}))
    }
    async fn cancel(&self, req: Request<ActionRef>) -> Result<Response<Empty>, Status> {
        self.engine()?.cancel(&req.into_inner().action_id);
        Ok(Response::new(Empty {}))
    }
    async fn release(&self, req: Request<ActionRef>) -> Result<Response<Empty>, Status> {
        let e = self.engine()?;
        let id = req.into_inner().action_id;
        e.cancel(&id);
        e.tracks
            .lock()
            .unwrap()
            .retain(|_, v| v.spec.action_id != id);
        Ok(Response::new(Empty {}))
    }
    async fn input(
        &self,
        req: Request<tonic::Streaming<MediaPacket>>,
    ) -> Result<Response<Empty>, Status> {
        let e = self.engine()?;
        let mut stream = req.into_inner();
        let first = stream.message().await?.ok_or_else(|| err("empty stream"))?;
        let t = e
            .tracks
            .lock()
            .unwrap()
            .values()
            .find(|t| t.spec.stream_id == first.stream_id)
            .cloned()
            .ok_or_else(|| err("unknown stream"))?;
        if t.input_claimed.swap(true, Ordering::SeqCst) {
            return Err(err("stream already connected"));
        }
        let source = t.input.clone().ok_or_else(|| err("track is an asset"))?;
        let size = validate_format(t.spec.format.as_ref().unwrap()).map_err(err)?;
        let mut order = PacketOrder::default();
        let mut next = Some(first);
        let result: Result<(), Status> = async {
            while let Some(packet) = next {
                if t.canceled.load(Ordering::SeqCst) || e.stopped.load(Ordering::SeqCst) {
                    return Err(err("stream canceled"));
                }
                if packet.stream_id != t.spec.stream_id {
                    return Err(err("stream identity changed"));
                }
                order.accept(&packet, size).map_err(err)?;
                if packet.end {
                    source.end_of_stream().map_err(err)?;
                    return Ok(());
                }
                let mut b = gst::Buffer::from_mut_slice(packet.data);
                b.get_mut()
                    .unwrap()
                    .set_pts(gst::ClockTime::from_useconds(packet.pts_us));
                let src = source.clone();
                tokio::task::spawn_blocking(move || src.push_buffer(b))
                    .await
                    .map_err(err)?
                    .map_err(err)?;
                next = stream.message().await?;
            }
            Err(err("producer disconnected before EOS"))
        }
        .await;
        if result.is_err() {
            t.cancel();
        }
        result?;
        Ok(Response::new(Empty {}))
    }
    type CaptureStream = RpcStream<MediaPacket>;
    async fn capture(
        &self,
        req: Request<CaptureRequest>,
    ) -> Result<Response<Self::CaptureStream>, Status> {
        let e = self.engine()?;
        let r = req.into_inner();
        let audio = r.kind == "audio";
        if (audio && !e.config.audio) || (!audio && (r.kind != "video" || !e.config.visual)) {
            return Err(err("capture capability disabled"));
        }
        if audio && ![0, 1, 2].contains(&r.channels) {
            return Err(err("invalid channels"));
        }
        let mut rx = if audio {
            e.audio.subscribe()
        } else {
            e.video.subscribe()
        };
        let (tx, out) = mpsc::channel(1);
        tokio::spawn(async move {
            let mut last = None;
            let mut gap = false;
            loop {
                if e.stopped.load(Ordering::SeqCst) {
                    break;
                }
                let receive = tokio::time::timeout(Duration::from_millis(250), rx.recv()).await;
                let result = match receive {
                    Err(_) => continue,
                    Ok(Ok(mut p)) => {
                        if !audio
                            && r.fps > 0
                            && last.is_some_and(|t| p.pts_us < t + 1_000_000 / r.fps.min(30) as u64)
                        {
                            continue;
                        }
                        last = Some(p.pts_us);
                        p.discontinuity = gap;
                        gap = false;
                        if audio && r.channels == 1 {
                            p.data = p
                                .data
                                .chunks_exact(4)
                                .flat_map(|s| {
                                    ((i16::from_le_bytes([s[0], s[1]]) as i32
                                        + i16::from_le_bytes([s[2], s[3]]) as i32)
                                        / 2)
                                    .to_le_bytes()[..2]
                                        .to_vec()
                                })
                                .collect();
                            p.format.as_mut().unwrap().channels = 1;
                        }
                        Ok(p)
                    }
                    Ok(Err(broadcast::error::RecvError::Lagged(n))) => {
                        e.emit(
                            "capture.gap",
                            serde_json::json!({"stream":r.kind,"dropped":n}),
                            e.now(),
                        );
                        if audio {
                            Err(Status::resource_exhausted("audio subscriber overrun"))
                        } else {
                            gap = true;
                            continue;
                        }
                    }
                    Ok(Err(_)) => break,
                };
                let failed = result.is_err();
                if tx.send(result).await.is_err() || failed {
                    break;
                }
            }
        });
        Ok(Response::new(Box::pin(ReceiverStream::new(out))))
    }
    type ObserveStream = RpcStream<Observation>;
    async fn observe(&self, _: Request<Empty>) -> Result<Response<Self::ObserveStream>, Status> {
        let rx = self.events.subscribe();
        let stream = tokio_stream::wrappers::BroadcastStream::new(rx)
            .map(|v| v.map_err(|_| Status::resource_exhausted("media event subscriber overrun")));
        Ok(Response::new(Box::pin(stream)))
    }
    async fn record(&self, req: Request<RecordingRequest>) -> Result<Response<Empty>, Status> {
        let e = self.engine()?;
        let enabled = req.into_inner().enabled;
        let notify = e.clone();
        tokio::task::spawn_blocking(move || e.record(enabled))
            .await
            .map_err(err)?
            .map_err(err)?;
        notify.emit(
            "recording.state",
            serde_json::json!({"enabled":enabled}),
            notify.now(),
        );
        Ok(Response::new(Empty {}))
    }
    async fn stop(&self, _: Request<Empty>) -> Result<Response<Empty>, Status> {
        let e = self.engine()?;
        let check = e.clone();
        tokio::task::spawn_blocking(move || e.stop())
            .await
            .map_err(err)?;
        if let Some(error) = check.error.lock().unwrap().clone() {
            return Err(err(error));
        }
        Ok(Response::new(Empty {}))
    }
}
pub async fn serve() -> Result<()> {
    let path = std::env::args()
        .nth(1)
        .ok_or_else(|| anyhow!("usage: mba-media SOCKET"))?;
    let listener = tokio::net::UnixListener::bind(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    let (events, _) = broadcast::channel(1024);
    let service = Service {
        engine: Arc::new(Mutex::new(None)),
        epoch: Instant::now(),
        events,
    };
    let cleanup = service.clone();
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tonic::transport::Server::builder()
        .add_service(
            media_server::MediaServer::new(service).max_decoding_message_size(8 * 1024 * 1024),
        )
        .serve_with_incoming_shutdown(UnixListenerStream::new(listener), async move {
            tokio::select! {_=term.recv()=>{},_=tokio::signal::ctrl_c()=>{}}
            if let Ok(e) = cleanup.engine() {
                e.stop();
            }
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn isolated(root: &std::path::Path) -> Arc<Engine> {
        let (events, _) = broadcast::channel(1024);
        let (audio, _) = broadcast::channel(50);
        let (video, _) = broadcast::channel(3);
        Arc::new(Engine {
            epoch: Instant::now(),
            stopped: AtomicBool::new(false),
            error: Mutex::new(None),
            devices: Mutex::new(None),
            outputs: Mutex::new(HashMap::new()),
            tracks: Mutex::new(HashMap::new()),
            captures: Mutex::new(vec![]),
            recordings: Mutex::new(vec![]),
            recording: AtomicBool::new(false),
            recording_origin: AtomicU64::new(0),
            recording_end: AtomicU64::new(0),
            events,
            audio,
            video,
            sequence: AtomicU64::new(0),
            config: MediaStart {
                root: root.to_str().unwrap().into(),
                ..Default::default()
            },
            wav: Mutex::new(HashMap::new()),
            wav_threads: Mutex::new(vec![]),
            display_frames: AtomicU64::new(0),
            display_gaps: AtomicU64::new(0),
            canceled: Mutex::new(HashSet::new()),
            fragments: Mutex::new(HashMap::new()),
        })
    }
    fn directory(name: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("mba-{name}-{}", std::process::id()));
        fs::create_dir_all(&p).unwrap();
        p
    }
    #[test]
    fn real_gstreamer_decodes_and_resamples_audio() {
        gst::init().unwrap();
        let root = directory("decode");
        let path = root.join("input.wav");
        let mut w = hound::WavWriter::create(
            &path,
            hound::WavSpec {
                channels: 1,
                sample_rate: 24000,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            },
        )
        .unwrap();
        for i in 0..24000 {
            w.write_sample(
                (10000.0 * (i as f32 * 440.0 * std::f32::consts::TAU / 24000.0).sin()) as i16,
            )
            .unwrap();
        }
        w.finalize().unwrap();
        let track = TrackState::create(Track {
            action_id: "a".into(),
            kind: "audio".into(),
            path: path.to_str().unwrap().into(),
            ..Default::default()
        })
        .unwrap();
        let mut output = track.first.lock().unwrap().take().unwrap();
        loop {
            if let Some(data) = track.pull().unwrap() {
                if data.is_empty() {
                    break;
                }
                output.extend(data);
            }
        }
        assert!(output.len() > 95000 && output.len() <= 96000);
        assert!(output
            .chunks_exact(2)
            .any(|s| i16::from_le_bytes([s[0], s[1]]).abs() > 9000));
        track.cancel();
        assert!(track.pull().is_err());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn wav_recording_chunks_and_final_tail_are_real_files() {
        let root = directory("record");
        fs::create_dir_all(root.join("recordings")).unwrap();
        let e = isolated(&root);
        let mut events = e.events.subscribe();
        e.start_wav("speaker", 2).unwrap();
        e.record_pcm("speaker", vec![0; 48000 * 4 * 11], 123_000);
        e.finish_recordings().unwrap();
        let mut counts = vec![];
        while let Ok(event) = events.try_recv() {
            let data: serde_json::Value = serde_json::from_str(&event.json).unwrap();
            let r = hound::WavReader::open(data["path"].as_str().unwrap()).unwrap();
            counts.push(r.duration());
            assert_eq!(r.spec().channels, 2);
        }
        assert_eq!(counts, vec![480000, 48000]);
        assert!(e.error.lock().unwrap().is_none());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn video_recording_finalizes_a_decodable_webm() {
        gst::init().unwrap();
        let root = directory("video-record");
        let mut e = isolated(&root);
        Arc::get_mut(&mut e).unwrap().config.visual = true;
        let mut events = e.events.subscribe();
        e.record(true).unwrap();
        let frame = [255u8, 0, 0].repeat(1280 * 720);
        for _ in 0..12 {
            e.record_frame(&frame, e.now());
            std::thread::sleep(Duration::from_millis(34));
        }
        e.record(false).unwrap();
        let event = events.try_recv().expect("final fragment was not published");
        assert_eq!(event.r#type, "recording.segment.available");
        let data: serde_json::Value = serde_json::from_str(&event.json).unwrap();
        let path = data["path"].as_str().unwrap();
        assert!(fs::metadata(path).unwrap().len() > 0);
        let decoder = pipeline("filesrc name=file ! matroskademux ! vp8dec ! videoconvert ! video/x-raw,format=RGB ! appsink name=decoded sync=false").unwrap();
        decoder
            .0
            .by_name("file")
            .unwrap()
            .set_property("location", path);
        decoder.0.set_state(gst::State::Playing).unwrap();
        let sink = decoder
            .0
            .by_name("decoded")
            .unwrap()
            .downcast::<AppSink>()
            .unwrap();
        let mut frames = 0;
        while sink
            .try_pull_sample(gst::ClockTime::from_seconds(5))
            .is_some()
        {
            frames += 1;
        }
        assert!(sink.is_eos());
        assert_eq!(frames, 12);
        assert!(e.error.lock().unwrap().is_none());
        drop(decoder);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn combined_recording_contains_video_and_both_timed_audio_sources() {
        gst::init().unwrap();
        let root = directory("combined-record");
        let mut e = isolated(&root);
        Arc::get_mut(&mut e).unwrap().config.visual = true;
        Arc::get_mut(&mut e).unwrap().config.audio = true;
        let mut events = e.events.subscribe();
        e.record(true).unwrap();
        let origin = e.recording_origin.load(Ordering::SeqCst);
        let frame = [255u8, 0, 0].repeat(1280 * 720);
        let tone = |index: usize, hz: f64, on: bool, channels: usize| -> Vec<u8> {
            (0..960)
                .flat_map(|i| {
                    let value = if on {
                        (8000.0
                            * (std::f64::consts::TAU * hz * (index * 960 + i) as f64 / 48000.0)
                                .sin()) as i16
                    } else {
                        0
                    };
                    (0..channels).flat_map(move |_| value.to_le_bytes())
                })
                .collect()
        };
        let mut frame_index = 0;
        for index in 0..60 {
            let at = origin + index as u64 * 20_000;
            e.record_pcm(
                "speaker",
                tone(index, 440.0, index < 25 || index >= 50, 2),
                at,
            );
            e.record_pcm("injected", tone(index, 880.0, index >= 25, 1), at);
            if index as u64 * 20_000 >= frame_index * 1_000_000 / 30 {
                e.record_frame(&frame, origin + frame_index * 1_000_000 / 30);
                frame_index += 1;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        e.record(false).unwrap();
        let mut full = None;
        while let Ok(event) = events.try_recv() {
            if event.r#type == "recording.available" {
                full = Some(serde_json::from_str::<serde_json::Value>(&event.json).unwrap());
            }
        }
        let full = full.expect("combined recording was not published");
        assert_eq!(
            full["audio_streams"],
            serde_json::json!(["speaker", "injected"])
        );
        let decoder = pipeline("filesrc name=file ! matroskademux name=d d.video_0 ! queue ! vp8dec ! appsink name=video sync=false d.audio_0 ! queue ! opusdec ! audioconvert ! audio/x-raw,format=S16LE,rate=48000,channels=1 ! appsink name=audio sync=false").unwrap();
        decoder
            .0
            .by_name("file")
            .unwrap()
            .set_property("location", full["path"].as_str().unwrap());
        playing(&decoder).unwrap();
        let video = decoder
            .0
            .by_name("video")
            .unwrap()
            .downcast::<AppSink>()
            .unwrap();
        let audio = decoder
            .0
            .by_name("audio")
            .unwrap()
            .downcast::<AppSink>()
            .unwrap();
        let mut count = 0;
        while video
            .try_pull_sample(gst::ClockTime::from_seconds(5))
            .is_some()
        {
            count += 1;
        }
        assert!(video.is_eos());
        assert_eq!(count, frame_index);
        let mut samples = Vec::new();
        while let Some(sample) = audio.try_pull_sample(gst::ClockTime::from_seconds(5)) {
            let data = sample.buffer().unwrap().map_readable().unwrap();
            samples.extend(
                data.as_slice()
                    .chunks_exact(2)
                    .map(|s| i16::from_le_bytes([s[0], s[1]]) as f64),
            );
        }
        assert!(audio.is_eos());
        assert!(
            samples.len() >= 57000 && samples.len() <= 58000,
            "{} audio samples",
            samples.len()
        );
        let amplitude = |start: usize, hz: f64| -> f64 {
            let values = &samples[start..start + 4800];
            let (sin, cos) = values
                .iter()
                .enumerate()
                .fold((0.0, 0.0), |(s, c), (i, x)| {
                    let angle = std::f64::consts::TAU * hz * i as f64 / 48000.0;
                    (s + x * angle.sin(), c + x * angle.cos())
                });
            2.0 * (sin * sin + cos * cos).sqrt() / 4800.0
        };
        assert!(amplitude(9600, 440.0) > 2000.0);
        assert!(amplitude(9600, 880.0) < 500.0);
        assert!(amplitude(33600, 880.0) > 2000.0);
        assert!(amplitude(33600, 440.0) < 500.0);
        assert!(amplitude(50400, 440.0) > 2000.0 && amplitude(50400, 880.0) > 2000.0);
        assert!(e.error.lock().unwrap().is_none());
        drop(decoder);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn real_gstreamer_converts_live_rgb_camera_frames() {
        gst::init().unwrap();
        let f = MediaFormat {
            kind: "video".into(),
            encoding: "RGB24".into(),
            width: 320,
            height: 240,
            fps: 30,
            ..Default::default()
        };
        let track = TrackState::create(Track {
            action_id: "v".into(),
            kind: "video".into(),
            stream_id: "stream".into(),
            format: Some(f),
            ..Default::default()
        })
        .unwrap();
        let src = track.input.as_ref().unwrap();
        let mut b = gst::Buffer::from_mut_slice([255u8, 0, 0].repeat(320 * 240));
        b.get_mut().unwrap().set_pts(gst::ClockTime::ZERO);
        src.push_buffer(b).unwrap();
        src.end_of_stream().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(data) = track.pull().unwrap() {
                assert_eq!(data.len(), VIDEO_SIZE);
                break;
            }
            assert!(Instant::now() < deadline);
        }
        track.cancel();
    }
}

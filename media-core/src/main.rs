#[cfg(feature = "native")]
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    mba_media::native::serve().await
}
#[cfg(not(feature = "native"))]
fn main() {
    eprintln!("This build has no native media backend. Rebuild with default features and GStreamer installed.");
    std::process::exit(1);
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    tonic_build::configure()
        .build_client(true)
        .compile_protos(&["../proto/adapter.proto"], &["../proto"])?;
    let output = std::path::PathBuf::from(std::env::var("OUT_DIR")?).join("mba.v1.rs");
    let checked = std::path::Path::new("src/generated.rs");
    if std::env::var_os("MBA_UPDATE_GENERATED").is_some() {
        std::fs::copy(&output, checked)?;
    } else if std::fs::read(&output)? != std::fs::read(checked)? {
        return Err("Rust protocol drift: run tools/generate.py --rust".into());
    }
    println!("cargo:rerun-if-env-changed=MBA_UPDATE_GENERATED");
    println!("cargo:rerun-if-changed=src/generated.rs");
    println!("cargo:rerun-if-changed=../proto/adapter.proto");
    Ok(())
}

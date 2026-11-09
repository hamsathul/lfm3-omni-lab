// The Windows program carries its icon and name as a resource; other systems need nothing here.
fn main() {
    println!("cargo:rerun-if-changed=assets/AppIcon.ico");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("assets/AppIcon.ico")
            .set("ProductName", "LFM3-Omni")
            .set("FileDescription", "LFM3-Omni")
            .set("CompanyName", "LFM3-Omni Lab");
        res.compile().expect("couldn't embed the Windows icon (it needs the Windows SDK's rc.exe)");
    }
}

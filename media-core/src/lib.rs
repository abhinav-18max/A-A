pub mod wire {
    tonic::include_proto!("mba.v1");
}

pub fn validate_format(f: &wire::MediaFormat) -> Result<usize, &'static str> {
    match f.kind.as_str() {
        "audio"
            if f.encoding == "S16LE"
                && [16000, 24000, 48000].contains(&f.rate)
                && [1, 2].contains(&f.channels) =>
        {
            Ok((f.rate / 50 * f.channels * 2) as usize)
        }
        "video"
            if ["RGB24", "YUY2"].contains(&f.encoding.as_str())
                && f.width > 0
                && f.width <= 1920
                && f.height > 0
                && f.height <= 1080
                && f.width % 2 == 0
                && (f.encoding != "RGB24" || f.width % 4 == 0)
                && f.fps > 0
                && f.fps <= 60 =>
        {
            Ok((f.width * f.height * if f.encoding == "RGB24" { 3 } else { 2 }) as usize)
        }
        _ => Err("unsupported media format"),
    }
}

#[derive(Default)]
pub struct PacketOrder {
    next: u64,
    last_pts: Option<u64>,
}
impl PacketOrder {
    pub fn accept(&mut self, packet: &wire::MediaPacket, size: usize) -> Result<(), &'static str> {
        if packet.sequence != self.next || self.last_pts.is_some_and(|pts| packet.pts_us <= pts) {
            return Err("invalid packet order");
        }
        if !packet.end && packet.data.len() != size {
            return Err("invalid packet size");
        }
        self.next += 1;
        self.last_pts = Some(packet.pts_us);
        Ok(())
    }
}

#[cfg(feature = "native")]
pub mod native;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_formats_and_packet_order() {
        let f = wire::MediaFormat {
            kind: "audio".into(),
            encoding: "S16LE".into(),
            rate: 24000,
            channels: 1,
            ..Default::default()
        };
        assert_eq!(validate_format(&f), Ok(960));
        let mut order = PacketOrder::default();
        let p = wire::MediaPacket {
            data: vec![0; 960],
            ..Default::default()
        };
        assert!(order.accept(&p, 960).is_ok());
        assert!(order.accept(&p, 960).is_err());
        assert!(validate_format(&wire::MediaFormat { rate: 44100, ..f }).is_err());
    }
}

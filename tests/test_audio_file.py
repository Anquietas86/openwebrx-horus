"""
End-to-end test: decode a Horus signal from a WAV file.

Usage:
    python test_audio_file.py <path_to_wav>

The WAV file should contain a Horus 4FSK signal at 48 kHz sample rate.
Test recordings are available from the horusdemodlib repo.
"""

import sys
import wave

from horusdemodlib.demod import HorusLib, Mode


def decode_wav(wav_path: str):
    print(f"Opening: {wav_path}")

    with wave.open(wav_path, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()

        print(f"  Channels: {channels}")
        print(f"  Sample width: {sample_width} bytes")
        print(f"  Sample rate: {framerate} Hz")
        print(f"  Frames: {n_frames}")
        print(f"  Duration: {n_frames / framerate:.1f}s")
        print()

        if sample_width != 2:
            print("ERROR: Expected 16-bit audio")
            return

        demod = HorusLib(
            mode=Mode.BINARY,
            sample_rate=framerate,
            stereo_iq=(channels == 2),
            verbose=False,
        )

        decoded = []
        chunk_frames = framerate // 10  # 100ms chunks
        chunk_bytes = chunk_frames * channels * sample_width

        while True:
            raw = wf.readframes(chunk_frames)
            if not raw:
                break

            frame = demod.add_samples(raw)
            if frame is not None:
                from horusdemodlib.decoder import decode_packet

                if frame.crc_pass:
                    telemetry = decode_packet(frame.data)
                    decoded.append(telemetry)
                    print(f"  DECODE: {telemetry.get('callsign', '???')} "
                          f"seq={telemetry.get('sequence_number')} "
                          f"lat={telemetry.get('latitude', 0):.5f} "
                          f"lon={telemetry.get('longitude', 0):.5f} "
                          f"alt={telemetry.get('altitude', 0)}m "
                          f"SNR={frame.snr:.1f}dB")
                else:
                    print(f"  CRC FAIL (SNR: {frame.snr:.1f}dB)")

        demod.close()
        print(f"\nDecoded {len(decoded)} valid packets")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_audio_file.py <wav_file>")
        print()
        print("Test WAV files with Horus 4FSK signals can be found at:")
        print("  https://github.com/projecthorus/horusdemodlib/tree/master/test")
        sys.exit(1)

    decode_wav(sys.argv[1])

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wb_frames"))
import qframes as frames  # noqa: E402

EXACT = frames.Profile(channelThreshold=0, maxChangedFraction=0.0, maxMeanAbsolute=0.0)
LOOSE = frames.Profile(channelThreshold=8, maxChangedFraction=0.01, maxMeanAbsolute=1.0)


def save(directory: Path, name: str, pixels: np.ndarray) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels.astype(np.uint8), "RGB").save(directory / name)


def gradient(width: int = 64, height: int = 48) -> np.ndarray:
    x = np.linspace(0, 255, width, dtype=np.uint8)
    return np.stack([np.tile(x, (height, 1))] * 3, axis=2)


class FramesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.reference = self.root / "reference"
        self.candidate = self.root / "candidate"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_identical_frames_pass_the_exact_profile(self) -> None:
        save(self.reference, "a.png", gradient())
        save(self.candidate, "a.png", gradient())
        report = frames.compare(self.reference, self.candidate, EXACT, None)
        self.assertTrue(report["passed"])
        self.assertEqual(report["frames"][0]["maxAbsolute"], 0)

    def test_one_changed_pixel_fails_the_exact_profile(self) -> None:
        changed = gradient()
        changed[10, 10] = [255, 0, 0]
        save(self.reference, "a.png", gradient())
        save(self.candidate, "a.png", changed)
        report = frames.compare(self.reference, self.candidate, EXACT, None)
        self.assertFalse(report["passed"])
        self.assertAlmostEqual(report["frames"][0]["changedFraction"], 1 / (64 * 48))

    def test_small_differences_pass_a_tolerant_profile(self) -> None:
        nudged = gradient().astype(np.int16)
        nudged[:, :, 1] = np.clip(nudged[:, :, 1] + 3, 0, 255)
        save(self.reference, "a.png", gradient())
        save(self.candidate, "a.png", nudged)
        self.assertFalse(frames.compare(self.reference, self.candidate, EXACT, None)["passed"])
        self.assertTrue(frames.compare(self.reference, self.candidate, LOOSE, None)["passed"])

    def test_a_changed_region_fails_a_tolerant_profile(self) -> None:
        changed = gradient()
        changed[:24, :32] = 0
        save(self.reference, "a.png", gradient())
        save(self.candidate, "a.png", changed)
        self.assertFalse(frames.compare(self.reference, self.candidate, LOOSE, None)["passed"])

    def test_missing_extra_and_resized_frames_fail(self) -> None:
        save(self.reference, "a.png", gradient())
        save(self.reference, "b.png", gradient())
        save(self.candidate, "a.png", gradient(32, 24))
        save(self.candidate, "c.png", gradient())
        report = frames.compare(self.reference, self.candidate, LOOSE, None)
        reasons = {frame["frame"]: frame.get("reason", "") for frame in report["frames"]}
        self.assertFalse(report["passed"])
        self.assertIn("sizes differ", reasons["a.png"])
        self.assertEqual(reasons["b.png"], "missing from candidate")
        self.assertEqual(reasons["c.png"], "not in reference")

    def test_no_frames_is_not_a_pass(self) -> None:
        self.reference.mkdir()
        self.candidate.mkdir()
        self.assertFalse(frames.compare(self.reference, self.candidate, EXACT, None)["passed"])

    def test_failed_frames_get_a_difference_image_and_a_report(self) -> None:
        changed = gradient()
        changed[:10, :10] = 0
        save(self.reference, "a.png", gradient())
        save(self.candidate, "a.png", changed)
        out = self.root / "out"
        frames.compare(self.reference, self.candidate, EXACT, out)
        self.assertTrue((out / "a.png").is_file())
        self.assertTrue((out / "report.json").is_file())

    def test_measure_reports_the_worst_difference_across_runs(self) -> None:
        second = gradient()
        second[0, 0] = [0, 200, 0]
        third = gradient()
        third[:4, :4] = 255
        for run, pixels in (("r1", gradient()), ("r2", second), ("r3", third)):
            save(self.root / run, "a.png", pixels)
        report = frames.measure_runs([self.root / "r1", self.root / "r2", self.root / "r3"], 8)
        self.assertEqual(report["pairs"], 2)
        self.assertEqual(report["identicalPairs"], 0)
        self.assertAlmostEqual(report["worst"]["changedFraction"], 16 / (64 * 48))

    def test_a_frame_captured_facing_elsewhere_is_suspect_and_a_settling_difference_is_not(self) -> None:
        settled = gradient()
        settling = gradient()
        settling[0:4, 0:16] = [0, 0, 0]
        elsewhere = np.flip(gradient(), axis=1)
        save(self.reference, "a.png", settled)
        save(self.reference, "b.png", settled)
        save(self.candidate, "a.png", settling)
        save(self.candidate, "b.png", elsewhere)
        found = frames.suspects(self.candidate, self.reference)
        self.assertEqual([frame["frame"] for frame in found], ["b.png"])
        self.assertGreater(found[0]["changedFraction"], 0.5)

    def test_check_passes_a_run_that_matches_and_fails_one_that_does_not(self) -> None:
        save(self.reference, "a.png", gradient())
        save(self.candidate, "a.png", gradient())
        self.assertEqual(frames.main(["check", str(self.candidate), str(self.reference)]), 0)
        save(self.candidate, "a.png", np.flip(gradient(), axis=1))
        self.assertEqual(frames.main(["check", str(self.candidate), str(self.reference)]), 1)


class QualityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def checker(size: int = 48, cell: int = 4) -> np.ndarray:
        y, x = np.mgrid[0:size, 0:size]
        board = (((x // cell) + (y // cell)) % 2 * 255).astype(np.uint8)
        return np.stack([board] * 3, axis=2)

    def test_identical_images_score_one_and_an_infinite_ratio(self) -> None:
        image = self.checker()
        self.assertAlmostEqual(frames.ssim(image.astype(np.int16), image.astype(np.int16)), 1.0, places=12)
        self.assertEqual(frames.psnr(image, image), float("inf"))
        self.assertAlmostEqual(frames.edge_detail(image, image), 1.0)

    def test_a_blurred_image_keeps_less_structure_and_less_detail(self) -> None:
        image = self.checker().astype(np.float64)
        # A 3 by 3 box blur, edges replicated.
        padded = np.pad(image, ((1, 1), (1, 1), (0, 0)), mode="edge")
        blurred = sum(padded[dy:dy + 48, dx:dx + 48] for dy in range(3) for dx in range(3)) / 9
        score = frames.ssim(image, blurred)
        self.assertLess(score, 0.9)
        self.assertGreater(score, 0.0)
        self.assertLess(frames.edge_detail(image, blurred), 0.8)
        self.assertLess(frames.psnr(image, blurred), 20)

    def test_a_small_shift_in_brightness_keeps_the_structure(self) -> None:
        image = self.checker().astype(np.int16) // 2 + 40
        self.assertGreater(frames.ssim(image, image + 10), 0.99)

    def test_more_noise_scores_lower(self) -> None:
        rng = np.random.default_rng(7)
        image = self.checker().astype(np.float64)
        light = image + rng.normal(0, 8, image.shape)
        heavy = image + rng.normal(0, 40, image.shape)
        self.assertGreater(frames.ssim(image, light), frames.ssim(image, heavy))

    def test_quality_scores_the_shared_frames_and_names_the_rest(self) -> None:
        save(self.root / "reference", "a.png", self.checker())
        save(self.root / "reference", "b.png", self.checker())
        save(self.root / "candidate", "a.png", self.checker())
        report = frames.quality(self.root / "reference", self.root / "candidate")
        self.assertEqual([row["frame"] for row in report["frames"]], ["a.png"])
        self.assertEqual(report["missing"], ["b.png"])
        self.assertAlmostEqual(report["mean"]["ssim"], 1.0, places=12)


if __name__ == "__main__":
    unittest.main()


class ShiftTest(unittest.TestCase):
    @staticmethod
    def texture(height: int, width: int, sigma: float, seed: int = 7) -> np.ndarray:
        """Noise smoothed to a cutoff of sigma cycles a pixel: structure at several scales, as terrain has."""
        rng = np.random.default_rng(seed)
        spectrum = np.fft.fft2(rng.normal(0, 1, (height, width)))
        fy = np.fft.fftfreq(height)[:, None]
        fx = np.fft.fftfreq(width)[None, :]
        spectrum *= np.exp(-(fx ** 2 + fy ** 2) / (2 * sigma ** 2))
        image = np.fft.ifft2(spectrum).real
        image = (image - image.min()) / (image.max() - image.min()) * 200 + 20
        return np.stack([image] * 3, axis=2)

    @staticmethod
    def moved(image: np.ndarray, dx: float, dy: float) -> np.ndarray:
        """The picture moved right by dx and down by dy, by a phase ramp."""
        height, width = image.shape[:2]
        fy = np.fft.fftfreq(height)[:, None]
        fx = np.fft.fftfreq(width)[None, :]
        ramp = np.exp(-2j * np.pi * (fx * dx + fy * dy))
        return np.stack([np.fft.ifft2(np.fft.fft2(image[..., c]) * ramp).real for c in range(3)], axis=2)

    @staticmethod
    def crop(image: np.ndarray) -> np.ndarray:
        """The middle of a larger picture, so content enters at the edges as it does between two frames."""
        return image[40:-40, 40:-40]

    def test_a_sub_pixel_shift_is_found_to_a_twentieth_of_a_pixel(self) -> None:
        for sigma in (0.08, 0.25):
            big = self.texture(400, 640, sigma)
            reference = self.crop(big)
            for dx, dy in [(0.3, -0.7), (-1.25, 0.4), (0.1, 0.1), (0.0, 0.0)]:
                found = frames.shift(reference, self.crop(self.moved(big, dx, dy)))
                self.assertAlmostEqual(found[0], dx, delta=0.06, msg=f"sigma {sigma}")
                self.assertAlmostEqual(found[1], dy, delta=0.06, msg=f"sigma {sigma}")

    def test_a_symmetric_blur_does_not_move_the_picture(self) -> None:
        big = self.texture(400, 640, 0.25)
        padded = np.pad(big, ((1, 1), (1, 1), (0, 0)), mode="edge")
        blurred = sum(padded[dy:dy + big.shape[0], dx:dx + big.shape[1]] for dy in range(3) for dx in range(3)) / 9
        found = frames.shift(self.crop(big), self.crop(self.moved(blurred, 0.45, -0.2)))
        self.assertAlmostEqual(found[0], 0.45, delta=0.06)
        self.assertAlmostEqual(found[1], -0.2, delta=0.06)

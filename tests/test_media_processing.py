import unittest
from io import BytesIO

from PIL import Image

from app.services.media_processing import process_uploaded_image_bytes


class MediaProcessingTests(unittest.TestCase):
    def test_jpeg_output_from_rgba_image(self):
        image = Image.new("RGBA", (16, 16), (255, 0, 0, 0))
        png_bytes = BytesIO()
        image.save(png_bytes, format="PNG")

        data, content_type, ext = process_uploaded_image_bytes(
            png_bytes.getvalue(),
            "transparent.jpg",
        )

        self.assertEqual(ext, "jpg")
        self.assertEqual(content_type, "image/jpeg")
        self.assertGreater(len(data), 0)

        with Image.open(BytesIO(data)) as output_image:
            self.assertEqual(output_image.mode, "RGB")


if __name__ == "__main__":
    unittest.main()

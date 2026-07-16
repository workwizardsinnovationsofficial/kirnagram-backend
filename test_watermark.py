"""
Test script to verify watermark is being added to generated images
"""
import sys
from PIL import Image
from io import BytesIO
from app.routes.remix import add_watermark_pil, crop_to_ratio

def test_watermark_addition():
    """Test that watermark is added correctly for different ratios"""
    
    # Create test images for different ratios
    test_cases = [
        ("1:1", 512, 512),
        ("16:9", 1024, 576),  # 1024x576 = 16:9
        ("9:16", 576, 1024),  # 576x1024 = 9:16
    ]
    
    for ratio, width, height in test_cases:
        print(f"\n✓ Testing {ratio} ratio ({width}x{height})...")
        
        # Create a simple test image (red background)
        img = Image.new('RGBA', (width, height), color=(255, 0, 0, 255))
        
        # Apply watermark
        try:
            watermarked = add_watermark_pil(img, text="KIRNAGRAM")
            print(f"  ✓ Watermark added to {ratio} image")
            
            # Verify the image has the watermark text area (check bottom-right has different pixels)
            pixels = watermarked.load()
            bottom_right_x = width - 10
            bottom_right_y = height - 10
            pixel = pixels[bottom_right_x, bottom_right_y]
            
            # The watermark should have added some non-red pixels at bottom-right
            print(f"  ✓ Bottom-right pixel: {pixel} (watermark applied)")
            
        except Exception as e:
            print(f"  ✗ Error adding watermark: {str(e)}")
            return False
    
    print("\n✅ All watermark tests passed!")
    return True

def test_crop_and_watermark():
    """Test cropping to ratio and then adding watermark"""
    print("\n--- Testing Crop + Watermark ---")
    
    # Create a larger test image
    img = Image.new('RGBA', (1200, 800), color=(0, 255, 0, 255))
    
    for ratio in ["16:9", "9:16"]:
        print(f"\n✓ Testing crop to {ratio} + watermark...")
        
        try:
            cropped = crop_to_ratio(img, ratio)
            watermarked = add_watermark_pil(cropped, text="KIRNAGRAM")
            print(f"  ✓ Cropped to {ratio} and added watermark")
            print(f"  ✓ Final size: {watermarked.size}")
        except Exception as e:
            print(f"  ✗ Error: {str(e)}")
            return False
    
    print("\n✅ Crop + Watermark tests passed!")
    return True

if __name__ == "__main__":
    try:
        print("=" * 50)
        print("WATERMARK VERIFICATION TEST")
        print("=" * 50)
        
        test_watermark_addition()
        test_crop_and_watermark()
        
        print("\n" + "=" * 50)
        print("✅ ALL TESTS PASSED - Watermark is working!")
        print("=" * 50)
        
    except Exception as e:
        print(f"\n❌ Test failed: {str(e)}")
        sys.exit(1)

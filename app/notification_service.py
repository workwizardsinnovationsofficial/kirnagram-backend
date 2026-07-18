import aiohttp
from typing import Dict
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.config import (
    FAST2SMS_API_KEY,
    FAST2SMS_ROUTE,
    FAST2SMS_SENDER_ID,
    FAST2SMS_ENTITY_ID,
    FAST2SMS_TEMPLATE_ID,
    SMTP_SERVER,
    SMTP_PORT,
    SMTP_EMAIL,
    SMTP_PASSWORD,
)

# Fast2SMS Configuration
FAST2SMS_API_KEY = FAST2SMS_API_KEY
FAST2SMS_ROUTE = FAST2SMS_ROUTE
FAST2SMS_SENDER_ID = FAST2SMS_SENDER_ID

# Email Configuration
SMTP_SERVER = SMTP_SERVER
SMTP_PORT = SMTP_PORT
SENDER_EMAIL = SMTP_EMAIL
SENDER_PASSWORD = SMTP_PASSWORD


class Fast2SMSService:
    """Service for sending SMS via Fast2SMS"""

    @staticmethod
    async def send_otp_sms(phone_number: str, otp: str) -> Dict:
        """Send OTP via SMS using Fast2SMS"""

        # Keep only digits
        digits = "".join(filter(str.isdigit, phone_number))

        # Convert to 10-digit mobile number
        if len(digits) > 10:
            digits = digits[-10:]

        # Validate number
        if len(digits) != 10:
            return {
                "success": False,
                "message": "Invalid phone number format. Must be 10 digits."
            }

        try:
            url = "https://www.fast2sms.com/dev/bulkV2"

            params = {
    "route": "dlt",
    "sender_id": FAST2SMS_SENDER_ID,
    "message": "212444",
    "variables_values": otp,
    "numbers": digits,
}

            headers = {
                "Authorization": FAST2SMS_API_KEY,
                "accept": "application/json",
                "cache-control": "no-cache",
            }

            print("=" * 60)
            print("API KEY:", FAST2SMS_API_KEY[:10] + "...")
            print("ENTITY ID:", FAST2SMS_ENTITY_ID)
            print("SENDER ID:", FAST2SMS_SENDER_ID)
            print("TEMPLATE ID:", FAST2SMS_TEMPLATE_ID)
            print("ROUTE:", FAST2SMS_ROUTE)
            print("OTP:", otp)
            print("NUMBER:", digits)
            print("=" * 60)

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                ) as response:

                    print("=" * 60)
                    print("FAST2SMS STATUS:", response.status)

                    response_text = await response.text()

                    print("FAST2SMS RESPONSE:")
                    print(response_text)
                    print("=" * 60)

                    if response.status == 200:
                        data = await response.json()

                        print("FAST2SMS JSON:", data)

                        if data.get("return") is True:
                            return {
                                "success": True,
                                "message": "OTP sent successfully",
                                "request_id": data.get("request_id"),
                            }

                        return {
                            "success": False,
                            "message": data.get(
                                "message",
                                "Failed to send OTP",
                            ),
                        }

                    return {
                        "success": False,
                        "message": response_text,
                    }

        except Exception as e:
            print("FAST2SMS EXCEPTION:", str(e))

            return {
                "success": False,
                "message": str(e),
            }

    @staticmethod
    async def send_custom_sms(phone_number: str, message: str) -> Dict:
        """Send custom SMS message via Fast2SMS"""

        digits = "".join(filter(str.isdigit, phone_number))

        if len(digits) > 10:
            digits = digits[-10:]

        if len(digits) != 10:
            return {
                "success": False,
                "message": "Invalid phone number format",
            }

        try:
            async with aiohttp.ClientSession() as session:
                url = "https://www.fast2sms.com/dev/bulkV2"

                params = {
                    "route": "q",
                    "message": message,
                    "numbers": digits,
                }

                headers = {
                    "Authorization": FAST2SMS_API_KEY,
                    "accept": "application/json",
                    "cache-control": "no-cache",
                }

                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                ) as response:

                    if response.status == 200:
                        data = await response.json()

                        return {
                            "success": data.get("return") is True,
                            "message": data.get("message", "Message sent"),
                        }

                    return {
                        "success": False,
                        "message": f"API error: {response.status}",
                    }

        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to send SMS: {str(e)}",
            }

class EmailService:
    """Service for sending emails"""
    
    @staticmethod
    async def send_otp_email(email: str, otp: str) -> Dict:
        """Send OTP via email"""
        
        subject = "Your KIRNAGRAM OTP"
        html_content = f"""
        <html>
            <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
                <div style="max-width: 600px; margin: 0 auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                    <h2 style="color: #333; text-align: center;">Welcome to KIRNAGRAM</h2>
                    
                    <p style="color: #666; font-size: 16px; margin: 20px 0;">
                        Your OTP for login is:
                    </p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <div style="font-size: 36px; font-weight: bold; color: #FF6B6B; letter-spacing: 5px;">
                            {otp}
                        </div>
                    </div>
                    
                    <p style="color: #999; font-size: 12px; text-align: center;">
                        This OTP is valid for 10 minutes. Do not share this with anyone.
                    </p>
                    
                    <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                    
                    <p style="color: #999; font-size: 12px; text-align: center;">
                        If you didn't request this OTP, please ignore this email.
                    </p>
                </div>
            </body>
        </html>
        """
        
        return await EmailService._send_email(email, subject, html_content)
    
    @staticmethod
    async def send_welcomme_email(email: str, full_name: str) -> Dict:
        """Send welcome email"""
        
        subject = "Welcome to KIRNAGRAM!"
        html_content = f"""
        <html>
            <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
                <div style="max-width: 600px; margin: 0 auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                    <h2 style="color: #333; text-align: center;">Welcome to KIRNAGRAM, {full_name}!</h2>
                    
                    <p style="color: #666; font-size: 16px; margin: 20px 0;">
                        Your account has been successfully created. We're excited to have you on board!
                    </p>
                    
                    <p style="color: #666; font-size: 16px; margin: 20px 0;">
                        Start exploring and sharing your moments with the community.
                    </p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="https://kirnagram.com" style="display: inline-block; background-color: #FF6B6B; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                            Get Started
                        </a>
                    </div>
                    
                    <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                    
                    <p style="color: #999; font-size: 12px; text-align: center;">
                        © 2024 KIRNAGRAM. All rights reserved.
                    </p>
                </div>
            </body>
        </html>
        """
        
        return await EmailService._send_email(email, subject, html_content)
    
    @staticmethod
    async def send_password_setup_notification(email: str, full_name: str) -> Dict:
        """Send password setup notification email"""
        
        subject = "Set Up a Password for Your Kirnagram Account"
        html_content = f"""
        <html>
            <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
                <div style="max-width: 600px; margin: 0 auto; background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                    <h2 style="color: #333; text-align: center;">Secure Your Account, {full_name}!</h2>
                    
                    <p style="color: #666; font-size: 16px; margin: 20px 0;">
                        We noticed you haven't set up a password for your Kirnagram account yet.
                    </p>
                    
                    <p style="color: #666; font-size: 16px; margin: 20px 0;">
                        For better security, we recommend setting up a strong password for your account.
                    </p>
                    
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="https://kirnagram.com/auth/setup-password" style="display: inline-block; background-color: #FF6B6B; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                            Set Up Password Now
                        </a>
                    </div>
                    
                    <p style="color: #999; font-size: 12px; text-align: center;">
                        This link will expire in 24 hours. You can set up a password anytime from your account settings.
                    </p>
                    
                    <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
                    
                    <p style="color: #999; font-size: 12px; text-align: center;">
                        If you need any help, please contact our support team.
                    </p>
                </div>
            </body>
        </html>
        """
        
        return await EmailService._send_email(email, subject, html_content)
    
    @staticmethod
    async def _send_email(to_email: str, subject: str, html_content: str) -> Dict:
        """Internal method to send email"""
        
        try:
            # Create message
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = SENDER_EMAIL
            message["To"] = to_email
            
            # Attach HTML content
            part = MIMEText(html_content, "html")
            message.attach(part)
            
            # Send email (using sync for now, can be made async if needed)
            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                    server.login(SENDER_EMAIL, SENDER_PASSWORD)
                    server.sendmail(SENDER_EMAIL, to_email, message.as_string())
            else:
                with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(SENDER_EMAIL, SENDER_PASSWORD)
                    server.sendmail(SENDER_EMAIL, to_email, message.as_string())
            
            return {
                "success": True,
                "message": "Email sent successfully"
            }
        
        except Exception as e:
            print(f"Email error: {str(e)}")
            return {
                "success": False,
                "message": f"Failed to send email: {str(e)}"
            }
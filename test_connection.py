import os
from canvasapi import Canvas
from dotenv import load_dotenv
load_dotenv()  # This loads the variables from .env
# Replace with your actual token
API_URL = os.getenv("CANVAS_API_URL")
API_KEY = os.getenv("CANVAS_API_KEY")

canvas = Canvas(API_URL, API_KEY)

try:
    user = canvas.get_current_user()
    print(f"Success! Connected as: {user.name}")
    
    courses = user.get_courses(enrollment_state='active')
    print("\nYour Active SVSU Courses:")
    for course in courses:
        if hasattr(course, 'name'):
            print(f"- {course.name} (ID: {course.id})")
except Exception as e:
    print(f"Error: {e}")
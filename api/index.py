import sys
import os

# Add root folder to python path to resolve imports from app.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

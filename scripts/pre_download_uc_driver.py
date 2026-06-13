import os
os.environ["DISPLAY"] = ":99"
from seleniumbase import SB
with SB(uc=True) as sb:
    pass
print("UC chromedriver pre-downloaded")

import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get('TOKEN', '')

questions = [
    {
        'word': 'Цезарь',
        'option1': 'салат',
        'option2': 'император',
    },
    {
        'word': 'Замок',
        'option1': 'слесарь',
        'option2': 'рыцарь',
    }
]

from django.db.models import TextChoices


class SubtitleStyle(TextChoices):
    CLASSIC = 'classic', 'Класичний'
    YELLOW = 'yellow', 'Жовтий акцент'
    DARK_BOX = 'dark-box', 'Темна плашка'
    CINEMA = 'cinema', 'Кіно'
    NEON = 'neon', 'Неон'
    STUDIO = 'studio', 'Студія'

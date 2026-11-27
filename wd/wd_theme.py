"""WiFiDesk 视觉 token 与 QSS 样式。"""
from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import Qt

DARK = {
    "background": "#101318",
    "surface": "#191E26",
    "elevated": "#222936",
    "border": "#2B3442",
    "text": "#EDF2F8",
    "subtext": "#A5B1C2",
    "accent": "#67A9FF",
    "success": "#55C99B",
    "warning": "#E8B86B",
    "danger": "#F27682",
    "shadow": "rgba(0,0,0,140)",
}

LIGHT = {
    "background": "#F5F7FA",
    "surface": "#FFFFFF",
    "elevated": "#EDF1F6",
    "border": "#DCE2EA",
    "text": "#182230",
    "subtext": "#526276",
    "accent": "#245CCB",
    "success": "#147C59",
    "warning": "#986300",
    "danger": "#BB3046",
    "shadow": "rgba(20,32,50,55)",
}


def tokens(theme: str) -> dict:
    return LIGHT if theme == "light" else DARK


def qss(theme: str) -> str:
    t = tokens(theme)
    return f"""
    QWidget {{
        background: {t['background']};
        color: {t['text']};
        font-family: "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", sans-serif;
        font-size: 15px;
    }}
    QFrame#card, QWidget#deviceRow, QWidget#toolbar {{
        background: {t['surface']};
        border: 1px solid {t['border']};
        border-radius: 10px;
    }}
    QWidget#titleBar {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {t['border']};
        border-radius: 0;
    }}
    QWidget#deviceRow:hover {{
        background: {t['elevated']};
        border-color: {t['accent']};
    }}
    QLabel#brand {{
        font-size: 18px;
        font-weight: 600;
        letter-spacing: 0.4px;
    }}
    QLabel#sectionTitle {{
        font-size: 18px;
        font-weight: 600;
    }}
    QLabel#subtext, QLabel#hint {{
        color: {t['subtext']};
        font-size: 14px;
    }}
    QLabel#statusOk {{ color: {t['success']}; }}
    QLabel#statusWarn {{ color: {t['warning']}; }}
    QLabel#statusDanger {{ color: {t['danger']}; }}
    QPushButton {{
        background: {t['elevated']};
        color: {t['text']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        padding: 7px 12px;
        min-height: 18px;
    }}
    QPushButton:hover {{ background: {t['border']}; }}
    QPushButton:pressed {{ background: {t['accent']}; color: #FFFFFF; }}
    QPushButton:disabled {{ color: {t['subtext']}; background: transparent; }}
    QPushButton#primary {{
        background: {t['accent']};
        color: #FFFFFF;
        border: 1px solid {t['accent']};
        font-weight: 600;
    }}
    QPushButton#primary:hover {{ background: #7FBAFF; }}
    QPushButton#danger {{ color: {t['danger']}; }}
    QPushButton#windowButton {{
        background: transparent;
        border: none;
        border-radius: 6px;
        padding: 4px 8px;
        font-size: 16px;
    }}
    QPushButton#windowButton:hover {{ background: {t['elevated']}; }}
    QPushButton#windowButtonClose:hover {{ background: {t['danger']}; color: #FFFFFF; }}
    QComboBox, QLineEdit, QSpinBox, QTextEdit {{
        background: {t['elevated']};
        color: {t['text']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        padding: 6px 8px;
        selection-background-color: {t['accent']};
    }}
    QComboBox::drop-down {{ border: none; }}
    QCheckBox, QRadioButton {{ spacing: 8px; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {t['border']}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {t['border']}; border-radius: 4px; min-width: 24px; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QLabel#mono {{
        font-family: "Cascadia Mono", "Consolas", monospace;
        letter-spacing: 1.4px;
        font-size: 17px;
        color: {t['accent']};
    }}
    QToolTip {{
        background: {t['elevated']};
        color: {t['text']};
        border: 1px solid {t['border']};
        padding: 4px 6px;
    }}
    """


def apply_theme(app, theme: str) -> None:
    t = tokens(theme)
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(t["background"]))
    palette.setColor(QPalette.WindowText, QColor(t["text"]))
    palette.setColor(QPalette.Base, QColor(t["surface"]))
    palette.setColor(QPalette.AlternateBase, QColor(t["elevated"]))
    palette.setColor(QPalette.Text, QColor(t["text"]))
    palette.setColor(QPalette.Button, QColor(t["elevated"]))
    palette.setColor(QPalette.ButtonText, QColor(t["text"]))
    palette.setColor(QPalette.Highlight, QColor(t["accent"]))
    palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(t["subtext"]))
    app.setPalette(palette)
    app.setStyleSheet(qss(theme))


import math
import pathlib
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
PIC_DIR = ROOT_DIR / "pic"

W, H = 1280, 720
S = 2
W2, H2 = W * S, H * S

FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
FONT_MONO_BOLD = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"

def draw_vector_check(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color: tuple, width: int = 4):
    p1 = (x, y + int(size * 0.46))
    p2 = (x + int(size * 0.36), y + int(size * 0.82))
    p3 = (x + size, y + int(size * 0.12))
    draw.line([p1, p2], fill=color, width=width)
    draw.line([p2, p3], fill=color, width=width)

def draw_emblem_icon(
    dd: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    icon_type: str,
    accent: tuple[int, int, int],
    S: int,
):
    if icon_type == "menu":
        bx1 = cx - int(72 * S)
        by1 = cy - int(66 * S)
        bx2 = cx + int(72 * S)
        by2 = cy + int(36 * S)
        dd.rounded_rectangle(
            [bx1, by1, bx2, by2],
            radius=int(24 * S),
            fill=(accent[0], accent[1], accent[2], 35),
            outline=accent,
            width=int(3.5 * S),
        )
        tail = [
            (bx1 + int(24 * S), by2),
            (bx1 + int(12 * S), by2 + int(22 * S)),
            (bx1 + int(44 * S), by2),
        ]
        dd.polygon(tail, fill=accent)

        dot_r = int(8 * S)
        dd.ellipse(
            [cx - dot_r, cy - int(15 * S) - dot_r, cx + dot_r, cy - int(15 * S) + dot_r],
            fill=(255, 255, 255, 250),
        )
        for r_arc, w_arc in [(int(25 * S), 3.2), (int(45 * S), 2.6)]:
            bbox = [
                cx - r_arc,
                cy - int(15 * S) - r_arc,
                cx + r_arc,
                cy - int(15 * S) + r_arc,
            ]
            dd.arc(bbox, start=-40, end=40, fill=accent, width=int(w_arc * S))
            dd.arc(bbox, start=140, end=220, fill=accent, width=int(w_arc * S))

    elif icon_type == "order":
        for tr in range(int(30 * S), 0, -int(5 * S)):
            dd.ellipse(
                [
                    cx - int(45 * S) - tr,
                    cy + int(25 * S) - tr,
                    cx - int(45 * S) + tr,
                    cy + int(25 * S) + tr,
                ],
                fill=(accent[0], accent[1], accent[2], int(100 * (1 - tr / (30 * S)))),
            )

        p_nose = (cx + int(68 * S), cy - int(32 * S))
        p_left = (cx - int(56 * S), cy - int(44 * S))
        p_mid = (cx - int(18 * S), cy + int(4 * S))
        p_right = (cx - int(34 * S), cy + int(56 * S))

        dd.polygon([p_nose, p_left, p_mid], fill=accent)
        dark_accent = (int(accent[0] * 0.75), int(accent[1] * 0.75), int(accent[2] * 0.75))
        dd.polygon([p_nose, p_mid, p_right], fill=dark_accent)
        dd.line([p_mid, p_nose], fill=(255, 255, 255, 220), width=int(2 * S))

    elif icon_type == "profile":
        hr = int(28 * S)
        hy = cy - int(32 * S)
        dd.ellipse([cx - hr, hy - hr, cx + hr, hy + hr], fill=accent)
        dd.ellipse(
            [cx - int(8 * S), hy - int(12 * S), cx + int(4 * S), hy],
            fill=(255, 255, 255, 140),
        )

        sb_x1 = cx - int(60 * S)
        sb_y1 = cy + int(12 * S)
        sb_x2 = cx + int(60 * S)
        sb_y2 = cy + int(72 * S)
        dd.chord(
            [sb_x1, sb_y1, sb_x2, sb_y2],
            start=180,
            end=0,
            fill=(accent[0], accent[1], accent[2], 220),
        )

        orb_r = int(82 * S)
        dd.arc(
            [cx - orb_r, cy - orb_r, cx + orb_r, cy + orb_r],
            start=-60,
            end=160,
            fill=(accent[0], accent[1], accent[2], 180),
            width=int(2.5 * S),
        )
        rad = math.radians(-40)
        dot_x = cx + int(orb_r * math.cos(rad))
        dot_y = cy + int(orb_r * math.sin(rad))
        dd.ellipse(
            [dot_x - int(7 * S), dot_y - int(7 * S), dot_x + int(7 * S), dot_y + int(7 * S)],
            fill=(255, 255, 255),
        )

    elif icon_type == "history":
        cr = int(72 * S)
        dd.ellipse(
            [cx - cr, cy - cr, cx + cr, cy + cr],
            outline=accent,
            width=int(3.5 * S),
        )
        for angle in [0, 90, 180, 270]:
            rad = math.radians(angle)
            x1 = cx + int((cr - int(14 * S)) * math.cos(rad))
            y1 = cy + int((cr - int(14 * S)) * math.sin(rad))
            x2 = cx + int((cr - int(4 * S)) * math.cos(rad))
            y2 = cy + int((cr - int(4 * S)) * math.sin(rad))
            dd.line([(x1, y1), (x2, y2)], fill=(255, 255, 255, 200), width=int(2.5 * S))

        rad_h = math.radians(220)
        hx = cx + int(34 * S * math.cos(rad_h))
        hy = cy + int(34 * S * math.sin(rad_h))
        dd.line([(cx, cy), (hx, hy)], fill=(255, 255, 255), width=int(4 * S))

        rad_m = math.radians(320)
        mx = cx + int(50 * S * math.cos(rad_m))
        my = cy + int(50 * S * math.sin(rad_m))
        dd.line([(cx, cy), (mx, my)], fill=accent, width=int(3.5 * S))

        dd.ellipse(
            [cx - int(7 * S), cy - int(7 * S), cx + int(7 * S), cy + int(7 * S)],
            fill=(255, 255, 255),
        )

    elif icon_type == "shield":
        p_top_l = (cx - int(60 * S), cy - int(65 * S))
        p_top_r = (cx + int(60 * S), cy - int(65 * S))
        p_mid_l = (cx - int(60 * S), cy - int(10 * S))
        p_mid_r = (cx + int(60 * S), cy - int(10 * S))
        p_bottom = (cx, cy + int(70 * S))

        shield_pts = [p_top_l, p_top_r, p_mid_r, p_bottom, p_mid_l]
        dd.polygon(shield_pts, fill=(accent[0], accent[1], accent[2], 40))
        dd.line([p_top_l, p_top_r], fill=accent, width=int(3.5 * S))
        dd.line([p_top_r, p_mid_r], fill=accent, width=int(3.5 * S))
        dd.line([p_mid_r, p_bottom], fill=accent, width=int(3.5 * S))
        dd.line([p_bottom, p_mid_l], fill=accent, width=int(3.5 * S))
        dd.line([p_mid_l, p_top_l], fill=accent, width=int(3.5 * S))

        draw_vector_check(
            dd,
            cx - int(24 * S),
            cy - int(24 * S),
            int(48 * S),
            (255, 255, 255),
            width=int(4.5 * S),
        )

    elif icon_type == "crown":
        p_base_l = (cx - int(65 * S), cy + int(42 * S))
        p_base_r = (cx + int(65 * S), cy + int(42 * S))
        p_peak_1 = (cx - int(65 * S), cy - int(35 * S))
        p_val_1 = (cx - int(34 * S), cy + int(6 * S))
        p_peak_2 = (cx - int(36 * S), cy - int(48 * S))
        p_val_2 = (cx, cy + int(10 * S))
        p_peak_center = (cx, cy - int(68 * S))
        p_peak_4 = (cx + int(36 * S), cy - int(48 * S))
        p_val_3 = (cx + int(34 * S), cy + int(6 * S))
        p_peak_5 = (cx + int(65 * S), cy - int(35 * S))

        crown_pts = [
            p_base_l,
            p_peak_1,
            p_val_1,
            p_peak_2,
            p_val_2,
            p_peak_center,
            p_val_2,
            p_peak_4,
            p_val_3,
            p_peak_5,
            p_base_r,
        ]
        dd.polygon(crown_pts, fill=(accent[0], accent[1], accent[2], 220))

        dd.rounded_rectangle(
            [cx - int(65 * S), cy + int(38 * S), cx + int(65 * S), cy + int(52 * S)],
            radius=int(6 * S),
            fill=(255, 255, 255, 240),
        )

        for pt in [p_peak_1, p_peak_2, p_peak_center, p_peak_4, p_peak_5]:
            dd.ellipse(
                [pt[0] - int(5 * S), pt[1] - int(5 * S), pt[0] + int(5 * S), pt[1] + int(5 * S)],
                fill=(255, 255, 255),
            )

    elif icon_type == "sliders":
        track_w = int(120 * S)
        slider_y = [cy - int(38 * S), cy, cy + int(38 * S)]
        knob_pos = [0.72, 0.38, 0.85]

        for sy, pos in zip(slider_y, knob_pos):
            x1 = cx - track_w // 2
            x2 = cx + track_w // 2
            dd.rounded_rectangle(
                [x1, sy - int(3 * S), x2, sy + int(3 * S)],
                radius=int(3 * S),
                fill=(255, 255, 255, 50),
            )
            kx = int(x1 + track_w * pos)
            dd.rounded_rectangle(
                [x1, sy - int(3 * S), kx, sy + int(3 * S)],
                radius=int(3 * S),
                fill=accent,
            )
            dd.ellipse(
                [kx - int(10 * S), sy - int(10 * S), kx + int(10 * S), sy + int(10 * S)],
                fill=(255, 255, 255),
            )
            dd.ellipse(
                [kx - int(5 * S), sy - int(5 * S), kx + int(5 * S), sy + int(5 * S)],
                fill=accent,
            )

    elif icon_type == "book":
        bw = int(58 * S)
        bh = int(72 * S)
        dd.rounded_rectangle(
            [cx - bw - int(4 * S), cy - bh // 2, cx - int(4 * S), cy + bh // 2],
            radius=int(8 * S),
            fill=(accent[0], accent[1], accent[2], 50),
            outline=accent,
            width=int(3 * S),
        )
        dd.rounded_rectangle(
            [cx + int(4 * S), cy - bh // 2, cx + bw + int(4 * S), cy + bh // 2],
            radius=int(8 * S),
            fill=(accent[0], accent[1], accent[2], 70),
            outline=accent,
            width=int(3 * S),
        )
        for ly in range(cy - int(20 * S), cy + int(24 * S), int(14 * S)):
            dd.line(
                [(cx + int(16 * S), ly), (cx + bw - int(12 * S), ly)],
                fill=(255, 255, 255, 180),
                width=int(2.5 * S),
            )

def render_clean_banner(
    section_title: str,
    subtitle: str,
    accent_rgb: tuple[int, int, int],
    icon_type: str,
    output_png: pathlib.Path,
    output_jpg: pathlib.Path | None = None,
):
    img2 = Image.new("RGBA", (W2, H2), (11, 14, 20, 255))

    glow = Image.new("RGBA", (W2, H2), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)

    cx, cy = int(890 * S), int(360 * S)

    for r in range(int(420 * S), 0, -int(18 * S)):
        factor = (1.0 - (r / (420 * S))) ** 2.2
        a = int(65 * factor)
        gd.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], a),
        )

    lx, ly = int(240 * S), int(290 * S)
    for r in range(int(300 * S), 0, -int(18 * S)):
        factor = (1.0 - (r / (300 * S))) ** 2.0
        a = int(26 * factor)
        gd.ellipse(
            [lx - r, ly - r, lx + r, ly + r],
            fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], a),
        )

    glow = glow.filter(ImageFilter.GaussianBlur(int(48 * S)))
    img2 = Image.alpha_composite(img2, glow)

    dd = ImageDraw.Draw(img2)

    for r_ring in [int(220 * S), int(320 * S), int(420 * S)]:
        dd.ellipse(
            [cx - r_ring, cy - r_ring, cx + r_ring, cy + r_ring],
            outline=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 22),
            width=int(1.5 * S),
        )

    emb_size = int(310 * S)
    ex1 = cx - emb_size // 2
    ey1 = cy - emb_size // 2
    ex2 = cx + emb_size // 2
    ey2 = cy + emb_size // 2
    radius = int(60 * S)

    shadow = Image.new("RGBA", (W2, H2), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle(
        [ex1 - int(8 * S), ey1 + int(14 * S), ex2 + int(8 * S), ey2 + int(26 * S)],
        radius=radius,
        fill=(0, 0, 0, 160),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(24 * S)))
    img2 = Image.alpha_composite(img2, shadow)

    dd = ImageDraw.Draw(img2)

    dd.rounded_rectangle(
        [ex1, ey1, ex2, ey2],
        radius=radius,
        fill=(18, 24, 38, 235),
        outline=(255, 255, 255, 35),
        width=int(2 * S),
    )
    dd.rounded_rectangle(
        [ex1 + int(3 * S), ey1 + int(3 * S), ex2 - int(3 * S), ey2 - int(3 * S)],
        radius=radius - int(3 * S),
        outline=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 70),
        width=int(1.5 * S),
    )
    dd.arc(
        [ex1 + int(14 * S), ey1 + int(6 * S), ex2 - int(14 * S), ey1 + int(48 * S)],
        start=190,
        end=350,
        fill=(255, 255, 255, 130),
        width=int(2.5 * S),
    )

    draw_emblem_icon(dd, cx, cy, icon_type, accent_rgb, S)

    f_brand = ImageFont.truetype(FONT_BOLD, int(52 * S))
    f_title = ImageFont.truetype(FONT_BOLD, int(42 * S))
    f_sub = ImageFont.truetype(FONT_REG, int(22 * S))

    tx = int(120 * S)
    ty = int(245 * S)

    dd.text((tx, ty), "Revolution", font=f_brand, fill=(255, 255, 255, 255))

    ty += int(78 * S)
    dd.text((tx, ty), section_title, font=f_title, fill=accent_rgb)

    ty += int(68 * S)
    dd.text((tx, ty), subtitle, font=f_sub, fill=(160, 175, 200, 220))

    final = img2.convert("RGB").resize((W, H), Image.Resampling.LANCZOS)
    output_png.parent.mkdir(parents=True, exist_ok=True)
    final.save(output_png, "PNG", optimize=True)
    if output_jpg:
        final.save(output_jpg, "JPEG", quality=95)
    print(f"Saved: {output_png.name}")

def build_avatar(output_png: pathlib.Path):
    size = 1000
    canvas = Image.new("RGBA", (size, size), (8, 10, 15, 255))
    cx, cy = size // 2, size // 2

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(400, 0, -10):
        factor = (1.0 - (r / 400)) ** 2.2
        a = int(45 * factor)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(56, 189, 248, a))
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    canvas.alpha_composite(glow)

    draw = ImageDraw.Draw(canvas)
    draw.ellipse([cx - 400, cy - 400, cx + 400, cy + 400], fill=(18, 22, 32, 255), outline=(56, 189, 248, 120), width=6)
    draw.ellipse([cx - 360, cy - 360, cx + 360, cy + 360], outline=(255, 255, 255, 30), width=2)

    font_huge = ImageFont.truetype(FONT_BOLD, 220)
    font_sub = ImageFont.truetype(FONT_MONO_BOLD, 36)

    bb_n = draw.textbbox((0, 0), "R", font=font_huge)
    nw = bb_n[2] - bb_n[0]
    nh = bb_n[3] - bb_n[1]
    draw.text((cx - nw // 2, cy - nh // 2 - 45), "R", font=font_huge, fill=(255, 255, 255))

    draw.ellipse([cx + nw // 2 + 10, cy - nh // 2 - 25, cx + nw // 2 + 34, cy - nh // 2 - 1], fill=(56, 189, 248))

    bb_sms = draw.textbbox((0, 0), "REVOLUTION", font=font_sub)
    sw = bb_sms[2] - bb_sms[0]
    draw.text((cx - sw // 2, cy + 120), "REVOLUTION", font=font_sub, fill=(140, 160, 185))

    canvas.save(output_png, "PNG", optimize=True)
    print(f"Saved: {output_png.name}")

def build_full_logo(output_png: pathlib.Path):
    W_l, H_l = 1200, 320
    canvas = Image.new("RGBA", (W_l, H_l), (8, 10, 15, 255))
    draw = ImageDraw.Draw(canvas)

    cx, cy, r = 160, 160, 80
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(18, 22, 32), outline=(56, 189, 248, 120), width=3)

    font_huge_n = ImageFont.truetype(FONT_BOLD, 64)
    bb_n = draw.textbbox((0, 0), "R", font=font_huge_n)
    nw = bb_n[2] - bb_n[0]
    nh = bb_n[3] - bb_n[1]
    draw.text((cx - nw // 2, cy - nh // 2 - 4), "R", font=font_huge_n, fill=(255, 255, 255))

    font_bold = ImageFont.truetype(FONT_BOLD, 72)
    font_sub = ImageFont.truetype(FONT_REG, 24)

    draw.text((280, 110), "Revolution", font=font_bold, fill=(255, 255, 255))
    draw.text((284, 188), "Сервис автоматической отправки сообщений", font=font_sub, fill=(140, 160, 185))

    canvas.save(output_png, "PNG", optimize=True)
    print(f"Saved: {output_png.name}")

def generate_all():
    PIC_DIR.mkdir(parents=True, exist_ok=True)

    render_clean_banner(
        section_title="Главное меню",
        subtitle="Быстрая и надёжная отправка сообщений",
        accent_rgb=(56, 189, 248),
        icon_type="menu",
        output_png=PIC_DIR / "banner_menu.png",
        output_jpg=PIC_DIR / "menu.jpg",
    )

    render_clean_banner(
        section_title="Новая сессия",
        subtitle="Настройка параметров и запуск отправки",
        accent_rgb=(16, 185, 129),
        icon_type="order",
        output_png=PIC_DIR / "banner_order.png",
        output_jpg=PIC_DIR / "order.jpg",
    )

    render_clean_banner(
        section_title="Мой профиль",
        subtitle="Информация об аккаунте и баланс звёзд",
        accent_rgb=(59, 130, 246),
        icon_type="profile",
        output_png=PIC_DIR / "banner_profile.png",
    )

    render_clean_banner(
        section_title="История запусков",
        subtitle="Журнал и статистика выполненных доставок",
        accent_rgb=(99, 102, 241),
        icon_type="history",
        output_png=PIC_DIR / "banner_history.png",
    )

    render_clean_banner(
        section_title="Защита номера",
        subtitle="Белый список и блокировка входящего спама",
        accent_rgb=(16, 185, 129),
        icon_type="shield",
        output_png=PIC_DIR / "banner_protect.png",
    )

    render_clean_banner(
        section_title="VIP доступ",
        subtitle="Премиальные привилегии и максимальный приоритет",
        accent_rgb=(245, 158, 11),
        icon_type="crown",
        output_png=PIC_DIR / "banner_vip.png",
    )

    render_clean_banner(
        section_title="Панель управления",
        subtitle="Мониторинг очередей и управление системой",
        accent_rgb=(244, 63, 94),
        icon_type="sliders",
        output_png=PIC_DIR / "banner_admin.png",
    )

    render_clean_banner(
        section_title="Добро пожаловать",
        subtitle="Руководство пользователя и правила сервиса",
        accent_rgb=(248, 250, 252),
        icon_type="book",
        output_png=PIC_DIR / "banner_welcome.png",
    )

    build_avatar(PIC_DIR / "logo_avatar.png")

    build_full_logo(PIC_DIR / "logo_full.png")

    print("All clean assets generated successfully!")

if __name__ == "__main__":
    generate_all()

import os
import io
import re
import json
import base64
import zipfile

import requests
from django.contrib.auth import authenticate, logout
from django.contrib.auth.models import User
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import DesignConfig, Conversation, Message, GeneratedInterface, MultiPageProject
from .serializers import (
    UserSerializer, RegisterSerializer, DesignConfigSerializer,
    ConversationSerializer, MessageSerializer, GeneratedInterfaceSerializer
)

# ── Constantes ─────────────────────────────────────────────────────────────────

ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
MODEL_CHAT = 'claude-haiku-4-5-20251001'
MODEL_MULTIPAGE = 'claude-haiku-4-5-20251001'


def get_anthropic_headers():
    return {
        'x-api-key': os.getenv('ANTHROPIC_API_KEY'),
        'anthropic-version': ANTHROPIC_VERSION,
        'content-type': 'application/json',
    }


# ── Utilidad: reparar JSON con HTML embebido ───────────────────────────────────

def repair_json_with_html(raw: str) -> dict:
    """
    Parser robusto para JSON con HTML embebido.
    Maneja comillas sin escapar dentro de html_code usando recorrido carácter a carácter.
    """
    text = raw.strip()

    # 1. Limpiar backticks de markdown
    if text.startswith('```'):
        lines = text.split('\n')
        text = '\n'.join(lines[1:])
        if text.endswith('```'):
            text = text[:-3].strip()

    # 2. Extraer desde primer '{'
    if not text.startswith('{'):
        idx = text.find('{')
        if idx != -1:
            text = text[idx:]

    # 3. Intento directo (cuando la IA escapa bien las comillas)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 4. Parser carácter a carácter — extrae html_code sin importar comillas sin escapar
    try:
        result = {}

        # Extraer project_title
        title_m = re.search(r'"project_title"\s*:\s*"([^"]*)"', text)
        if title_m:
            result['project_title'] = title_m.group(1)

        pages = []
        search_from = 0

        while True:
            # Buscar el próximo filename
            fn_m = re.search(r'"filename"\s*:\s*"([^"]+\.html)"', text[search_from:])
            if not fn_m:
                break

            abs_pos = search_from + fn_m.start()

            # Buscar title de la página (cerca del filename)
            title_m2 = re.search(r'"title"\s*:\s*"([^"]*)"', text[abs_pos:abs_pos + 300])
            page_title = title_m2.group(1) if title_m2 else fn_m.group(1)

            # Buscar "html_code" después del filename
            hc_pos = text.find('"html_code"', abs_pos)
            if hc_pos == -1:
                break

            # Saltar hasta el inicio del valor string (después de ": "
            colon = text.find(':', hc_pos + len('"html_code"'))
            if colon == -1:
                break
            quote = text.find('"', colon)
            if quote == -1:
                break

            content_start = quote + 1

            # Recorrer carácter a carácter para encontrar el fin real del string
            i = content_start
            html_chars = []
            while i < len(text):
                c = text[i]
                if c == '\\' and i + 1 < len(text):
                    nc = text[i + 1]
                    if nc == '"':
                        html_chars.append('"')
                    elif nc == 'n':
                        html_chars.append('\n')
                    elif nc == 't':
                        html_chars.append('\t')
                    elif nc == 'r':
                        html_chars.append('\r')
                    elif nc == '\\':
                        html_chars.append('\\')
                    else:
                        html_chars.append(nc)
                    i += 2
                    continue
                # Detectar fin del string: comilla seguida de , o } o whitespace+}
                if c == '"':
                    rest = text[i+1:i+10].lstrip()
                    if rest.startswith((',', '}', ']')):
                        break
                    # Si la comilla va seguida de otro campo JSON, también es fin
                    if re.match(r'\s*[,}\]]', text[i+1:i+5]):
                        break
                    # Comilla sin escapar dentro del HTML — la incluimos como carácter
                    html_chars.append(c)
                    i += 1
                    continue
                html_chars.append(c)
                i += 1

            html_content = ''.join(html_chars)

            if html_content.strip().startswith('<!'):
                pages.append({
                    'filename': fn_m.group(1),
                    'title': page_title,
                    'html_code': html_content
                })

            search_from = i + 1

        if pages:
            result['pages'] = pages
            return result

    except Exception:
        pass

    # 5. Último recurso: cerrar estructuras truncadas
    try:
        fixed = text
        open_b = fixed.count('{') - fixed.count('}')
        open_br = fixed.count('[') - fixed.count(']')
        fixed += '}' * max(0, open_b)
        fixed += ']' * max(0, open_br)
        return json.loads(fixed)
    except Exception:
        pass

    raise ValueError('No se pudo parsear la respuesta de la IA. Intenta con una descripción más corta.')

# ── Autenticación ──────────────────────────────────────────────────────────────

class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get('username', '').strip()
        password = request.data.get('password', '')
        if not username or not password:
            return Response({'error': 'Usuario y contraseña requeridos'}, status=status.HTTP_400_BAD_REQUEST)

        user = authenticate(request, username=username, password=password)
        if user:
            refresh = RefreshToken.for_user(user)
            return Response({
                'user': UserSerializer(user).data,
                'access': str(refresh.access_token),
                'refresh': str(refresh),
            })
        return Response({'error': 'Credenciales incorrectas'}, status=status.HTTP_400_BAD_REQUEST)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({'message': 'Sesión cerrada'})


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


# ── Conversaciones ─────────────────────────────────────────────────────────────

class ConversationListCreateView(generics.ListCreateAPIView):
    serializer_class = ConversationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Conversation.objects.filter(user=self.request.user).order_by('-updated_at')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ConversationDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = ConversationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Conversation.objects.filter(user=self.request.user)


# ── Generador de interfaz (chat) ───────────────────────────────────────────────

class GenerateInterfaceView(APIView):
    permission_classes = [IsAuthenticated]

    SYSTEM_PROMPT = """Eres Didactify AI, generador experto de interfaces web de nivel agencia. Tu salida debe ser indistinguible de un sitio diseñado por un profesional senior.

════════════════════════════════════
FORMATO DE RESPUESTA — MUY IMPORTANTE
════════════════════════════════════
Responde ÚNICAMENTE con este JSON. Sin texto antes ni después. Sin backticks. Sin comentarios:
{{"message":"frase breve en español sobre lo generado","html":"AQUI_EL_HTML_COMPLETO","suggestions":["sugerencia1","sugerencia2","sugerencia3"]}}

REGLA CRÍTICA JSON: El HTML va como valor del campo "html". Dentro del HTML NO uses comillas dobles " en ningún atributo — usa SOLO comillas simples ' en todos los atributos HTML.
CORRECTO:   <a href='#productos' class='btn'>Ver más</a>
INCORRECTO: <a href="#productos" class="btn">Ver más</a>
Esto es obligatorio para que el JSON sea válido. TODOS los atributos HTML usan comillas simples.

════════════════════════════════════
NAVEGACIÓN CON SCROLL SUAVE — OBLIGATORIO
════════════════════════════════════
1. El <html> siempre tiene: <html lang='es' style='scroll-behavior:smooth'>
2. Cada sección tiene id sin espacios: <section id='productos'>, <section id='contacto'>
3. Los links del nav apuntan a esos ids: <a href='#productos'>Productos</a>
4. NUNCA uses href='/' ni href='pagina.html' ni href='#' sin id real
5. El nav tiene position:sticky;top:0;z-index:100 para que siempre sea visible

════════════════════════════════════
DISEÑO VISUAL — NIVEL AGENCIA
════════════════════════════════════
CSS BASE obligatorio (reemplaza PRIMARIO/SECUNDARIO/ACENTO/ALPHA/FUENTE):
*{{box-sizing:border-box;margin:0;padding:0}}
html{{scroll-behavior:smooth}}
:root{{--c1:PRIMARIO;--c2:SECUNDARIO;--c3:ACENTO;--c1a:ALPHA;--w:#fff;--g:#f8f9fc;--g2:#eef2ff;--dk:#0f172a;--tx:#475569;--r:16px;--sh:0 4px 20px rgba(0,0,0,.08);--sh2:0 16px 48px rgba(0,0,0,.16)}}
body{{font-family:'FUENTE',sans-serif;color:var(--dk);background:var(--g);line-height:1.6}}
nav{{background:var(--dk);padding:.9rem 2rem;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100;box-shadow:0 2px 20px rgba(0,0,0,.25)}}
.logo{{color:var(--c3);font-size:1.2rem;font-weight:800;text-decoration:none;display:flex;align-items:center;gap:.5rem}}
nav ul{{list-style:none;display:flex;gap:.15rem;align-items:center}}
nav a{{color:rgba(255,255,255,.72);text-decoration:none;font-weight:600;font-size:.875rem;padding:.4rem .85rem;border-radius:8px;transition:all .2s}}
nav a:hover{{color:#fff;background:rgba(255,255,255,.12)}}
.nav-cta{{background:var(--c3)!important;color:var(--dk)!important;border-radius:999px!important;padding:.4rem 1.1rem!important}}
.hero{{background:linear-gradient(135deg,var(--c1) 0%,var(--c2) 100%);padding:6rem 2rem 5rem;text-align:center;color:#fff;position:relative;overflow:hidden}}
.hero::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 80% 60% at 70% 30%,rgba(255,255,255,.1) 0%,transparent 60%);pointer-events:none}}
.hero::after{{content:'';position:absolute;bottom:0;left:0;right:0;height:80px;background:var(--g);clip-path:ellipse(60% 100% at 50% 100%)}}
.hero-tag{{display:inline-block;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.35);padding:.35rem 1rem;border-radius:999px;font-size:.78rem;font-weight:700;margin-bottom:1.25rem;letter-spacing:.06em}}
.hero h1{{font-size:clamp(2.4rem,5.5vw,4rem);font-weight:800;line-height:1.1;max-width:780px;margin:0 auto 1.1rem;letter-spacing:-.02em}}
.hero p{{font-size:1.1rem;opacity:.88;max-width:580px;margin:0 auto 2.5rem;line-height:1.75}}
.hero-btns{{display:flex;gap:.75rem;justify-content:center;flex-wrap:wrap;position:relative;z-index:1}}
.btn{{display:inline-flex;align-items:center;gap:.4rem;padding:.85rem 2rem;background:var(--c3);color:var(--dk);font-weight:700;border-radius:999px;text-decoration:none;transition:all .22s;border:none;cursor:pointer;font-size:.92rem;font-family:inherit;letter-spacing:-.01em}}
.btn:hover{{transform:translateY(-3px);box-shadow:0 12px 30px rgba(0,0,0,.25);filter:brightness(1.08)}}
.btn-ghost{{background:transparent;color:#fff;border:2px solid rgba(255,255,255,.45)}}
.btn-ghost:hover{{border-color:#fff;background:rgba(255,255,255,.12)}}
.btn-dark{{background:var(--dk);color:#fff}}
.sec{{padding:5.5rem 2rem;max-width:1140px;margin:0 auto}}
.sec-alt{{background:var(--w);padding:5.5rem 2rem}}
.sec-alt .sec{{margin:0 auto}}
.sec-label{{display:inline-flex;align-items:center;gap:.4rem;background:var(--g2);color:var(--c1);padding:.3rem .9rem;border-radius:999px;font-size:.75rem;font-weight:700;border:1.5px solid var(--c1);margin-bottom:.75rem;letter-spacing:.04em}}
.sec-title{{font-size:clamp(1.8rem,3.2vw,2.6rem);font-weight:800;color:var(--dk);margin-bottom:.6rem;line-height:1.2;letter-spacing:-.02em}}
.sec-sub{{color:var(--tx);margin-bottom:3rem;font-size:1rem;line-height:1.75;max-width:560px}}
.g2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.75rem}}
.g3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1.5rem}}
.g4{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1.25rem}}
.card{{background:var(--w);border-radius:var(--r);padding:2rem;box-shadow:var(--sh);transition:all .28s;border:1px solid rgba(0,0,0,.05);position:relative;overflow:hidden}}
.card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--c1),var(--c3));opacity:0;transition:opacity .28s}}
.card:hover{{transform:translateY(-7px);box-shadow:var(--sh2)}}
.card:hover::before{{opacity:1}}
.ci{{font-size:2.75rem;margin-bottom:1.1rem;display:block;line-height:1}}
.card-badge{{display:inline-block;padding:.22rem .65rem;background:var(--g2);color:var(--c1);border-radius:6px;font-size:.72rem;font-weight:700;margin-bottom:.75rem}}
.card h3{{font-size:1.1rem;font-weight:800;margin-bottom:.5rem;color:var(--dk);letter-spacing:-.01em}}
.card p{{font-size:.875rem;color:var(--tx);line-height:1.7}}
.stats-row{{display:flex;flex-wrap:wrap;gap:1.5rem;justify-content:center;margin:2.5rem 0}}
.stat{{text-align:center;padding:2rem 2.25rem;background:var(--w);border-radius:var(--r);box-shadow:var(--sh);min-width:150px;border:1px solid rgba(0,0,0,.04);position:relative;overflow:hidden}}
.stat::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--c1),var(--c3))}}
.stat .n{{font-size:2.8rem;font-weight:800;color:var(--c1);line-height:1;letter-spacing:-.03em}}
.stat .l{{font-size:.8rem;color:var(--tx);margin-top:.4rem;font-weight:600}}
.prog-wrap{{height:8px;border-radius:4px;background:#e2e8f0;overflow:hidden;margin:.5rem 0 1rem}}
.prog{{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--c1),var(--c3))}}
.tag{{display:inline-block;padding:.28rem .75rem;background:var(--g2);color:var(--c1);border-radius:6px;font-size:.75rem;font-weight:600;margin:.2rem}}
.testi{{background:var(--w);border-radius:var(--r);padding:1.85rem;box-shadow:var(--sh);border:1px solid rgba(0,0,0,.05);position:relative}}
.testi::before{{content:'\\201C';position:absolute;top:.75rem;right:1.25rem;font-size:4rem;color:var(--c1);opacity:.12;font-family:Georgia,serif;line-height:1}}
.testi-text{{font-size:.9rem;color:var(--tx);line-height:1.75;margin-bottom:1.25rem;font-style:italic}}
.testi-author{{display:flex;align-items:center;gap:.75rem}}
.testi-avatar{{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,var(--c1),var(--c3));display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:1rem;flex-shrink:0}}
.testi-name{{font-weight:700;font-size:.875rem;color:var(--dk)}}
.testi-role{{font-size:.75rem;color:var(--tx)}}
.cta-section{{background:linear-gradient(135deg,var(--c1),var(--c2));padding:5rem 2rem;text-align:center;position:relative;overflow:hidden}}
.cta-section::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 80% at 80% 20%,rgba(255,255,255,.1),transparent 60%)}}
.cta-section h2{{font-size:clamp(1.8rem,3vw,2.6rem);font-weight:800;color:#fff;margin-bottom:1rem;letter-spacing:-.02em;position:relative}}
.cta-section p{{color:rgba(255,255,255,.85);font-size:1.05rem;margin-bottom:2.25rem;max-width:520px;margin-left:auto;margin-right:auto;line-height:1.7;position:relative}}
input,select,textarea{{width:100%;padding:.85rem 1.1rem;border:1.5px solid #e2e8f0;border-radius:10px;font-size:.9rem;margin-bottom:.9rem;outline:none;font-family:inherit;transition:all .2s;background:var(--w)}}
input:focus,select:focus,textarea:focus{{border-color:var(--c1);box-shadow:0 0 0 3px var(--c1a)}}
label{{display:block;font-weight:700;font-size:.82rem;margin-bottom:.35rem;color:var(--dk)}}
.form-card{{background:var(--w);padding:2.5rem;border-radius:var(--r);box-shadow:var(--sh2);max-width:580px;margin:0 auto}}
footer{{background:var(--dk);color:rgba(255,255,255,.5);padding:4rem 2rem 2rem}}
.footer-inner{{max-width:1140px;margin:0 auto;display:flex;flex-wrap:wrap;gap:2.5rem;justify-content:space-between;align-items:flex-start;margin-bottom:2.5rem}}
.f-logo{{color:var(--c3);font-weight:800;font-size:1.15rem;display:block;margin-bottom:.6rem;text-decoration:none}}
.f-desc{{font-size:.82rem;max-width:240px;line-height:1.7;margin-bottom:1rem}}
.f-social{{display:flex;gap:.5rem}}
.f-social a{{width:32px;height:32px;border-radius:8px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);display:flex;align-items:center;justify-content:center;color:rgba(255,255,255,.5);text-decoration:none;font-size:.8rem;transition:all .2s}}
.f-social a:hover{{background:rgba(255,255,255,.15);color:#fff}}
.f-links h4{{color:#fff;font-size:.82rem;margin-bottom:.85rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase}}
.f-links a{{display:block;color:rgba(255,255,255,.45);text-decoration:none;font-size:.82rem;margin-bottom:.45rem;transition:color .2s}}
.f-links a:hover{{color:var(--c3)}}
.footer-bar{{max-width:1140px;margin:0 auto;border-top:1px solid rgba(255,255,255,.08);padding-top:1.5rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem;font-size:.78rem}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(22px)}}to{{opacity:1;transform:translateY(0)}}}}
.hero h1,.hero p,.hero-btns{{animation:fadeUp .65s ease both}}
.hero p{{animation-delay:.12s}}.hero-btns{{animation-delay:.24s}}
@media(max-width:768px){{nav ul{{gap:0}}nav a{{padding:.32rem .55rem;font-size:.78rem}}.hero{{padding:4rem 1rem 3.5rem}}.hero::after{{display:none}}.sec{{padding:3.5rem 1rem}}.sec-alt{{padding:3.5rem 1rem}}.hero h1{{font-size:2rem}}.g3,.g2,.g4{{grid-template-columns:1fr}}.stats-row{{gap:1rem}}.footer-inner{{flex-direction:column}}}}

Script JS obligatorio al final del <body> — scroll animado + scroll-spy nav:
<script>
document.querySelectorAll('nav a[href^="#"]').forEach(a=>{{
  a.addEventListener('click',e=>{{
    e.preventDefault();
    const t=document.querySelector(a.getAttribute('href'));
    if(t) t.scrollIntoView({{behavior:'smooth',block:'start'}});
  }});
}});
const obs=new IntersectionObserver(entries=>entries.forEach(x=>{{
  if(x.isIntersecting){{x.target.style.animation='fadeUp .55s ease both';x.target.style.opacity='1'}}
}}),{{threshold:0.1}});
document.querySelectorAll('.card,.stat,.testi,.sec-title,.sec-label').forEach(el=>{{el.style.opacity='0';obs.observe(el)}});
<\/script>

════════════════════════════════════
PALETA — elige la más adecuada al tema del usuario
════════════════════════════════════
Deportes/Fitness: --c1:#1d4ed8;--c2:#1e3a8a;--c3:#facc15;--c1a:rgba(29,78,216,.1)
Restaurante/Food: --c1:#b45309;--c2:#92400e;--c3:#fcd34d;--c1a:rgba(180,83,9,.1)
Salud/Bienestar:  --c1:#059669;--c2:#065f46;--c3:#34d399;--c1a:rgba(5,150,105,.1)
Tecnología/SaaS:  --c1:#7c3aed;--c2:#4c1d95;--c3:#a78bfa;--c1a:rgba(124,58,237,.1)
Corporativo:      --c1:#1e40af;--c2:#1e3a8a;--c3:#60a5fa;--c1a:rgba(30,64,175,.1)
Creativo/Moda:    --c1:#db2777;--c2:#9d174d;--c3:#f472b6;--c1a:rgba(219,39,119,.1)
Naranja/Energía:  --c1:#ea580c;--c2:#c2410c;--c3:#fbbf24;--c1a:rgba(234,88,12,.1)
Educativo:        --c1:#3b82f6;--c2:#1d4ed8;--c3:#fbbf24;--c1a:rgba(59,130,246,.1)
Si el usuario pide colores específicos, úsalos respetando la estructura --c1/c2/c3.

Fuente según tono (importar en <head> con comillas simples en el href):
Deportes/Energía → Outfit | Moderno/Tech → Space+Grotesk | Amigable → Nunito o Poppins | Elegante → Raleway

════════════════════════════════════
ESTRUCTURA OBLIGATORIA — 8 secciones mínimo
════════════════════════════════════
1. <nav> sticky: logo emoji+nombre + ul con 4-5 <a href='#id'> + <a class='nav-cta btn'>CTA</a>
2. <section id='hero' class='hero'>: hero-tag + h1 grande + p + 2 botones. Con wave CSS al fondo.
3. <section id='stats'>: stats-row con 4 .stat (números grandes reales del negocio)
4. <section id='servicios'> o 'productos': grid .g3 con 4+ .card (emoji ci + badge + h3 + p de 2-3 líneas)
5. <section id='destacados'>: sección visual especial según el negocio (grid de productos, galería, proceso)
6. <section id='testimonios'>: 3 .testi con texto real de 2-3 líneas + avatar CSS + nombre + cargo
7. <section class='cta-section'>: h2 + p + 2 botones (uno .btn-ghost). Fondo gradiente.
8. <footer>: footer-inner con logo+f-desc+f-social + 3 .f-links columns + footer-bar

REGLA ANTI-VACÍO: Cada sección DEBE tener contenido inventado pero realista y específico al negocio.
NUNCA dejes secciones con solo título y subtítulo. NUNCA uses placeholder text genérico.

Estilo visual: {style} | Fuente sugerida: {font}{palette_hint}"""

    def post(self, request):
        user_message = request.data.get('message', '').strip()
        conversation_id = request.data.get('conversation_id')
        style = request.data.get('style', 'moderno')
        font = request.data.get('font', 'Inter')
        palette = request.data.get('palette')

        if not user_message:
            return Response({'error': 'Mensaje requerido'}, status=status.HTTP_400_BAD_REQUEST)

        # Obtener o crear conversación
        if conversation_id:
            try:
                conversation = Conversation.objects.get(id=conversation_id, user=request.user)
            except Conversation.DoesNotExist:
                return Response({'error': 'Conversación no encontrada'}, status=status.HTTP_404_NOT_FOUND)
        else:
            conversation = Conversation.objects.create(
                user=request.user,
                title=user_message[:60]
            )

        # Guardar mensaje del usuario
        Message.objects.create(conversation=conversation, role='user', content=user_message)

        # Historial de mensajes (máx últimos 10 para no inflar tokens)
        messages_history = [
            {'role': msg.role, 'content': msg.content}
            for msg in conversation.messages.order_by('created_at')[:10]
        ]

        palette_hint = f' | Paleta: {palette}' if palette else ''
        system = self.SYSTEM_PROMPT.format(style=style, font=font, palette_hint=palette_hint)

        try:
            api_resp = requests.post(
                ANTHROPIC_URL,
                headers=get_anthropic_headers(),
                json={
                    'model': MODEL_CHAT,
                    'max_tokens': 10000,
                    'system': system,
                    'messages': messages_history,
                },
                timeout=120
            )
            api_json = api_resp.json()
        except requests.Timeout:
            return Response({'error': 'Tiempo de espera agotado. Intenta de nuevo.'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
        except Exception as e:
            return Response({'error': f'Error de conexión: {str(e)}'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if 'content' not in api_json:
            return Response({'error': f'Error de API: {api_json.get("error", {}).get("message", "desconocido")}'}, status=status.HTTP_502_BAD_GATEWAY)

        raw_text = api_json['content'][0]['text']

        # ── Parser robusto para JSON con HTML embebido ──────────────────────────
        # json.loads falla cuando el HTML tiene comillas sin escapar.
        # Estrategia: extraer el valor de "html" con un parser carácter a carácter,
        # igual que repair_json_with_html, y luego parsear el resto normalmente.
        parsed = None

        # 1. Limpiar backticks de markdown
        clean = raw_text.strip()
        if clean.startswith('```'):
            lines = clean.split('\n')
            clean = '\n'.join(lines[1:])
            if clean.endswith('```'):
                clean = clean[:-3].strip()
        if not clean.startswith('{'):
            idx = clean.find('{')
            if idx != -1:
                clean = clean[idx:]

        # 2. Intento directo (cuando el modelo escapa bien las comillas)
        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            pass

        # 3. Parser carácter a carácter — extrae "html" sin importar comillas sin escapar
        if not parsed:
            try:
                result = {}

                # Extraer "message"
                msg_m = re.search(r'"message"\s*:\s*"([^"]*)"', clean)
                if msg_m:
                    result['message'] = msg_m.group(1)

                # Extraer "suggestions"
                sug_m = re.search(r'"suggestions"\s*:\s*\[([^\]]*)\]', clean)
                if sug_m:
                    sugs = re.findall(r'"([^"]+)"', sug_m.group(1))
                    result['suggestions'] = sugs

                # Extraer "html" con parser carácter a carácter
                hc_pos = clean.find('"html"')
                if hc_pos != -1:
                    colon = clean.find(':', hc_pos + 6)
                    quote = clean.find('"', colon)
                    if quote != -1:
                        i = quote + 1
                        html_chars = []
                        while i < len(clean):
                            c = clean[i]
                            if c == '\\' and i + 1 < len(clean):
                                nc = clean[i + 1]
                                if nc == '"': html_chars.append('"')
                                elif nc == 'n': html_chars.append('\n')
                                elif nc == 't': html_chars.append('\t')
                                elif nc == 'r': html_chars.append('\r')
                                elif nc == '\\': html_chars.append('\\')
                                else: html_chars.append(nc)
                                i += 2
                                continue
                            if c == '"':
                                rest = clean[i+1:i+10].lstrip()
                                if rest.startswith((',', '}', ']')):
                                    break
                                if re.match(r'\s*[,}\]]', clean[i+1:i+5]):
                                    break
                                html_chars.append(c)
                                i += 1
                                continue
                            html_chars.append(c)
                            i += 1
                        html_content = ''.join(html_chars)
                        if '<!DOCTYPE' in html_content or '<html' in html_content:
                            result['html'] = html_content

                if result.get('html'):
                    parsed = result
            except Exception:
                pass

        # 4. Último recurso — regex para extraer el bloque HTML directamente
        if not parsed or not parsed.get('html'):
            html_match = re.search(r'<!DOCTYPE\s+html[\s\S]*?</html>', raw_text, re.IGNORECASE)
            parsed = {
                'message': parsed.get('message', 'Interfaz generada.') if parsed else 'Interfaz generada.',
                'html': html_match.group(0) if html_match else '',
                'suggestions': parsed.get('suggestions', ['Cambiar colores', 'Agregar animaciones', 'Añadir más secciones']) if parsed else ['Cambiar colores', 'Agregar animaciones', 'Añadir más secciones']
            }

        # Guardar respuesta del asistente
        Message.objects.create(conversation=conversation, role='assistant', content=raw_text)

        return Response({
            'conversation_id': conversation.id,
            'message': parsed.get('message', ''),
            'html': parsed.get('html', ''),
            'suggestions': parsed.get('suggestions', [])
        })


# ── Interfaces guardadas ───────────────────────────────────────────────────────

class InterfaceListCreateView(generics.ListCreateAPIView):
    serializer_class = GeneratedInterfaceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return GeneratedInterface.objects.filter(user=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class InterfaceDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = GeneratedInterfaceSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return GeneratedInterface.objects.filter(user=self.request.user)


# ── Configuraciones de diseño ──────────────────────────────────────────────────

class DesignConfigListCreateView(generics.ListCreateAPIView):
    serializer_class = DesignConfigSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return DesignConfig.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


# ── Generador Multi-Página ─────────────────────────────────────────────────────

MULTIPAGE_SYSTEM_PROMPT = """Eres un diseñador web senior de nivel agencia. Generas sitios web multi-página de alta calidad visual, sin espacios vacíos, con contenido real y abundante.

════════════════════════════════════════
FORMATO DE RESPUESTA (estricto)
════════════════════════════════════════
- Responde SOLO con JSON válido. Sin texto extra. Sin backticks. Sin comentarios.
- Estructura exacta:
{"project_title":"Nombre","pages":[{"filename":"index.html","title":"Inicio","html_code":"<!DOCTYPE html>..."},{"filename":"pagina2.html","title":"Nombre","html_code":"<!DOCTYPE html>..."}]}
- index.html SIEMPRE es la primera página.
- Usa href relativos: href="contacto.html" (NUNCA href="/contacto" ni href="#")
- Genera EXACTAMENTE el número de páginas pedidas. Máximo 3 páginas.
- NUNCA combines varias páginas en una sola.
- REGLA CRÍTICA JSON: todas las comillas dentro de html_code DEBEN escaparse como \". Ejemplo: href=\"pagina.html\"

════════════════════════════════════════
CSS BASE — copia EXACTO en cada página
════════════════════════════════════════
*{box-sizing:border-box;margin:0;padding:0}
:root{--c1:PRIMARIO;--c2:SECUNDARIO;--c3:ACENTO;--c1a:PRIMARIO_ALPHA;--w:#fff;--g:#f8f9fc;--g2:#eef2ff;--dk:#0f172a;--dk2:#1e293b;--tx:#475569;--r:16px;--sh:0 4px 20px rgba(0,0,0,.08);--sh2:0 16px 48px rgba(0,0,0,.15)}
body{font-family:'FUENTE',sans-serif;color:var(--dk);background:var(--g);line-height:1.6}
nav{background:var(--dk);padding:.9rem 2rem;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100;box-shadow:0 2px 20px rgba(0,0,0,.2)}
.logo{color:var(--c3);font-size:1.2rem;font-weight:800;text-decoration:none}
nav ul{list-style:none;display:flex;gap:.25rem}
nav a{color:rgba(255,255,255,.7);text-decoration:none;font-weight:600;font-size:.875rem;padding:.4rem .85rem;border-radius:8px;transition:all .2s}
nav a:hover,nav a.active{color:#fff;background:rgba(255,255,255,.12)}
.nav-cta{background:var(--c3)!important;color:var(--dk)!important;border-radius:999px!important}
.hero{background:linear-gradient(135deg,var(--c1) 0%,var(--c2) 100%);padding:5.5rem 2rem 4.5rem;text-align:center;color:#fff;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at 70% 40%,rgba(255,255,255,.1) 0%,transparent 60%)}
.hero-tag{display:inline-block;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);padding:.3rem .9rem;border-radius:999px;font-size:.78rem;font-weight:700;margin-bottom:1.2rem;letter-spacing:.05em}
.hero h1{font-size:clamp(2.2rem,5vw,3.8rem);font-weight:800;line-height:1.12;max-width:750px;margin:0 auto 1rem}
.hero p{font-size:1.1rem;opacity:.85;max-width:580px;margin:0 auto 2.25rem;line-height:1.75}
.hero-btns{display:flex;gap:.75rem;justify-content:center;flex-wrap:wrap}
.btn{display:inline-block;padding:.8rem 1.9rem;background:var(--c3);color:var(--dk);font-weight:700;border-radius:999px;text-decoration:none;transition:all .22s;border:none;cursor:pointer;font-size:.92rem;font-family:inherit}
.btn:hover{transform:translateY(-3px);box-shadow:0 10px 28px rgba(0,0,0,.22);filter:brightness(1.08)}
.btn-ghost{background:transparent;color:#fff;border:2px solid rgba(255,255,255,.4)}
.btn-ghost:hover{border-color:#fff;background:rgba(255,255,255,.1)}
.btn-dark{background:var(--dk);color:#fff}
.sec{padding:5rem 2rem;max-width:1100px;margin:0 auto}
.sec-alt{background:var(--w);padding:5rem 2rem}
.sec-alt .sec{margin:0 auto}
.sec-label{display:inline-block;background:var(--g2);color:var(--c1);padding:.25rem .8rem;border-radius:999px;font-size:.75rem;font-weight:700;border:1.5px solid var(--c1);margin-bottom:.6rem;letter-spacing:.04em}
.sec-title{font-size:clamp(1.7rem,3vw,2.4rem);font-weight:800;color:var(--dk);margin-bottom:.5rem;line-height:1.25}
.sec-sub{color:var(--tx);margin-bottom:2.75rem;font-size:1rem;line-height:1.7;max-width:560px}
.g2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.75rem}
.g3{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1.5rem}
.g4{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1.25rem}
.card{background:var(--w);border-radius:var(--r);padding:1.85rem;box-shadow:var(--sh);transition:all .28s;border:1px solid rgba(0,0,0,.05);position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--c1),var(--c3));opacity:0;transition:opacity .28s}
.card:hover{transform:translateY(-6px);box-shadow:var(--sh2)}
.card:hover::before{opacity:1}
.ci{font-size:2.5rem;margin-bottom:1rem;display:block}
.card-badge{display:inline-block;padding:.2rem .65rem;background:var(--g2);color:var(--c1);border-radius:6px;font-size:.72rem;font-weight:700;margin-bottom:.75rem}
.card h3{font-size:1.05rem;font-weight:700;margin-bottom:.5rem;color:var(--dk)}
.card p{font-size:.875rem;color:var(--tx);line-height:1.65}
.stats-row{display:flex;flex-wrap:wrap;gap:1.5rem;justify-content:center;margin:2.5rem 0}
.stat{text-align:center;padding:1.75rem 2rem;background:var(--w);border-radius:var(--r);box-shadow:var(--sh);min-width:140px;border:1px solid rgba(0,0,0,.04)}
.stat .n{font-size:2.6rem;font-weight:800;color:var(--c1);line-height:1}
.stat .l{font-size:.78rem;color:var(--tx);margin-top:.35rem;font-weight:500}
.prog-wrap{height:8px;border-radius:4px;background:#e2e8f0;overflow:hidden;margin:.5rem 0 1rem}
.prog{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--c1),var(--c3))}
.tag{display:inline-block;padding:.25rem .7rem;background:var(--g2);color:var(--c1);border-radius:6px;font-size:.75rem;font-weight:600;margin:.2rem}
input,select,textarea{width:100%;padding:.8rem 1.1rem;border:1.5px solid #e2e8f0;border-radius:10px;font-size:.9rem;margin-bottom:.85rem;outline:none;font-family:inherit;transition:all .2s;background:var(--w)}
input:focus,select:focus,textarea:focus{border-color:var(--c1);box-shadow:0 0 0 3px var(--c1a)}
label{display:block;font-weight:600;font-size:.83rem;margin-bottom:.3rem;color:var(--dk)}
.form-card{background:var(--w);padding:2.5rem;border-radius:var(--r);box-shadow:var(--sh2);max-width:580px;margin:0 auto}
footer{background:var(--dk);color:rgba(255,255,255,.5);padding:3.5rem 2rem 2rem}
.footer-inner{max-width:1100px;margin:0 auto;display:flex;flex-wrap:wrap;gap:2rem;justify-content:space-between;align-items:flex-start;margin-bottom:2rem}
.f-logo{color:var(--c3);font-weight:800;font-size:1.1rem;display:block;margin-bottom:.5rem}
.f-desc{font-size:.82rem;max-width:240px;line-height:1.65}
.f-links h4{color:#fff;font-size:.85rem;margin-bottom:.75rem;font-weight:700}
.f-links a{display:block;color:rgba(255,255,255,.45);text-decoration:none;font-size:.82rem;margin-bottom:.4rem;transition:color .2s}
.f-links a:hover{color:var(--c3)}
.footer-bar{max-width:1100px;margin:0 auto;border-top:1px solid rgba(255,255,255,.08);padding-top:1.5rem;text-align:center;font-size:.8rem}
@keyframes fadeUp{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:translateY(0)}}
.hero h1,.hero p,.hero-btns{animation:fadeUp .6s ease both}
.hero p{animation-delay:.15s}.hero-btns{animation-delay:.28s}
@media(max-width:768px){nav ul{gap:.05rem}nav a{padding:.3rem .55rem;font-size:.8rem}.hero{padding:3.5rem 1rem 3rem}.sec{padding:3rem 1rem}.sec-alt{padding:3rem 1rem}.hero h1{font-size:1.9rem}.g3,.g2,.g4{grid-template-columns:1fr}}

════════════════════════════════════════
PALETAS — elige la más adecuada al tema
════════════════════════════════════════
Educativo:   --c1:#3b82f6;--c2:#1d4ed8;--c3:#fbbf24;--c1a:rgba(59,130,246,.1)
Restaurante: --c1:#b45309;--c2:#92400e;--c3:#fcd34d;--c1a:rgba(180,83,9,.1)
Salud:       --c1:#059669;--c2:#065f46;--c3:#34d399;--c1a:rgba(5,150,105,.1)
Tecnología:  --c1:#7c3aed;--c2:#4c1d95;--c3:#a78bfa;--c1a:rgba(124,58,237,.1)
Corporativo: --c1:#1e40af;--c2:#1e3a8a;--c3:#60a5fa;--c1a:rgba(30,64,175,.1)
Creativo:    --c1:#db2777;--c2:#9d174d;--c3:#f472b6;--c1a:rgba(219,39,119,.1)
Naranja:     --c1:#ea580c;--c2:#c2410c;--c3:#fbbf24;--c1a:rgba(234,88,12,.1)

TIPOGRAFÍA: importa en <head> la fuente más apropiada al tono:
Moderno/Tech → Space+Grotesk o Outfit | Amigable → Nunito o Poppins | Elegante → Raleway

════════════════════════════════════════
REGLA ANTI-ESPACIOS-VACÍOS (crítica)
════════════════════════════════════════
Cada sección DEBE estar llena de contenido real:
- Cards: mínimo 3 párrafos con 2-3 líneas de descripción cada uno. NUNCA solo título.
- Listas: mínimo 5 ítems. Cada ítem tiene nombre + descripción de 1-2 líneas.
- Testimonios: nombre completo + cargo + empresa + cita de 2-3 líneas.
- Proceso/Steps: 4 pasos con número grande, título y descripción de 2 líneas.
- Hero: badge + h1 + párrafo descriptivo de 2-3 líneas + 2 botones.
- Stats: 4 números con label descriptivo debajo.
- PROHIBIDO: secciones con solo título y subtítulo sin contenido real debajo.

════════════════════════════════════════
ESTRUCTURA MÍNIMA POR PÁGINA
════════════════════════════════════════
index.html (Home): nav + hero completo + stats(4) + features(3-4 cards) + sección destacada + testimonios(3) + CTA + footer
Páginas internas: nav + hero compacto (py:3rem) + 2-3 secciones ricas en contenido + CTA + footer

AGREGA al final de cada <body> este JS para animaciones de scroll:
<script>const o=new IntersectionObserver(e=>e.forEach(x=>{if(x.isIntersecting){x.target.style.animation='fadeUp .5s ease both';x.target.style.opacity='1'}}),{threshold:0.12});document.querySelectorAll('.card,.stat,.sec-title').forEach(el=>{el.style.opacity='0';o.observe(el)});</script>"""


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_multipage(request):
    prompt = request.data.get('prompt', '').strip()
    project_title = request.data.get('title', 'Mi Proyecto Web').strip()

    if not prompt:
        return Response({'error': 'El prompt es requerido'}, status=400)

    try:
        api_resp = requests.post(
            ANTHROPIC_URL,
            headers=get_anthropic_headers(),
            json={
                'model': MODEL_MULTIPAGE,
                'max_tokens': 12000,
                'system': MULTIPAGE_SYSTEM_PROMPT,
                'messages': [{'role': 'user', 'content': prompt}],
            },
            timeout=180
        )
        api_json = api_resp.json()

    except requests.Timeout:
        return Response({'error': 'Tiempo de espera agotado. Intenta con un proyecto más simple o menos páginas.'}, status=504)
    except Exception as e:
        return Response({'error': f'Error de conexión: {str(e)}'}, status=503)

    if 'content' not in api_json:
        err_msg = api_json.get('error', {}).get('message', str(api_json))
        return Response({'error': f'Error de API: {err_msg}'}, status=502)

    raw_text = api_json['content'][0]['text']

    try:
        data = repair_json_with_html(raw_text)
    except ValueError as e:
        return Response({'error': str(e)}, status=500)

    pages = data.get('pages', [])
    title = data.get('project_title', project_title)

    if not pages:
        return Response({'error': 'La IA no generó páginas. Intenta describir el proyecto con más detalle.'}, status=500)

    # Guardar en base de datos
    try:
        project = MultiPageProject.objects.create(
            user=request.user,
            title=title,
            description=prompt,
            pages=[{'filename': p['filename'], 'title': p['title']} for p in pages]
        )
    except Exception as e:
        return Response({'error': f'Error al guardar proyecto: {str(e)}'}, status=500)

    # Generar ZIP en memoria
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for page in pages:
            html = page.get('html_code', '')
            if html:
                zf.writestr(page['filename'], html)

    zip_buffer.seek(0)
    zip_b64 = base64.b64encode(zip_buffer.read()).decode('utf-8')
    zip_filename = re.sub(r'[^\w\-]', '_', title.lower()) + '.zip'

    return Response({
        'project_id': project.id,
        'title': title,
        'pages': [{'filename': p['filename'], 'title': p['title']} for p in pages],
        'pages_html': [{'filename': p['filename'], 'title': p['title'], 'html_code': p.get('html_code', '')} for p in pages],
        'zip_b64': zip_b64,
        'zip_filename': zip_filename
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_multipage_projects(request):
    projects = MultiPageProject.objects.filter(user=request.user).order_by('-created_at')
    data = [{
        'id': p.id,
        'title': p.title,
        'pages': p.pages,
        'created_at': p.created_at.strftime('%d/%m/%Y %H:%M'),
    } for p in projects]
    return Response(data)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_multipage_project(request, pk):
    try:
        project = MultiPageProject.objects.get(pk=pk, user=request.user)
        project.delete()
        return Response({'deleted': True})
    except MultiPageProject.DoesNotExist:
        return Response({'error': 'Proyecto no encontrado'}, status=404)

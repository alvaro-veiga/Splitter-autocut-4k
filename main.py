"""
Spliter autocut 4k
=====================
Aplicativo desktop local para detectar cortes de cena em vídeos e exportar
cada cena como um arquivo separado, preservando qualidade máxima (4K/original).

Stack:
    - customtkinter (GUI)
    - PySceneDetect (detecção de cenas via ContentDetector)
    - FFmpeg / FFprobe (via subprocess) para exportação
    - threading para não travar a interface\
By: Alvaro Veiga
"""

from __future__ import annotations

import csv
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from tkinter import filedialog, messagebox, PhotoImage

# PySceneDetect
try:
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import ContentDetector
except Exception as exc:  # pragma: no cover
    print("Erro ao importar PySceneDetect:", exc)
    raise


# =============================================================================
# Constantes e utilidades
# =============================================================================

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts"}

MODO_QUALIDADE = "Máxima Qualidade (CPU)"
MODO_RAPIDO = "Rápido (GPU)"
MODO_AUTO = "Automático"

ENCODER_AUTO = "Auto"
ENCODER_CPU = "CPU"
ENCODER_NVIDIA = "NVIDIA (NVENC)"
ENCODER_INTEL = "Intel (QSV)"
ENCODER_AMD = "AMD (AMF)"


def which_ffmpeg() -> Optional[str]:
    """Retorna caminho do ffmpeg ou None."""
    return shutil.which("ffmpeg")


def which_ffprobe() -> Optional[str]:
    """Retorna caminho do ffprobe ou None."""
    return shutil.which("ffprobe")


def run_cmd(cmd: list[str], timeout: Optional[int] = None) -> tuple[int, str, str]:
    """Executa comando e retorna (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return -1, "", str(exc)


def listar_encoders_ffmpeg() -> set[str]:
    """Lista encoders disponíveis no FFmpeg local."""
    if not which_ffmpeg():
        return set()
    rc, out, _ = run_cmd(["ffmpeg", "-hide_banner", "-encoders"])
    if rc != 0:
        return set()
    encoders: set[str] = set()
    for linha in out.splitlines():
        partes = linha.strip().split()
        if len(partes) >= 2 and partes[0].startswith("V"):
            encoders.add(partes[1])
    return encoders


def testar_encoder(nome_encoder: str) -> bool:
    """Testa se um encoder realmente funciona (evita 'listado mas quebrado')."""
    if not which_ffmpeg():
        return False
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=black:s=320x240:d=0.2",
        "-c:v", nome_encoder, "-frames:v", "3",
        "-f", "null", "-",
    ]
    rc, _, _ = run_cmd(cmd, timeout=15)
    return rc == 0


@dataclass
class EncoderInfo:
    """Info sobre um encoder de vídeo utilizável."""
    codec: str            # ex.: 'h264_nvenc', 'libx264'
    tipo: str             # 'gpu' ou 'cpu'
    familia: str          # 'nvidia', 'intel', 'amd', 'cpu'


def detectar_encoders_disponiveis() -> dict[str, EncoderInfo]:
    """Detecta encoders CPU/GPU que funcionam de fato."""
    disponiveis: dict[str, EncoderInfo] = {}
    encoders = listar_encoders_ffmpeg()

    # CPU sempre presente
    if "libx264" in encoders:
        disponiveis["cpu_h264"] = EncoderInfo("libx264", "cpu", "cpu")
    if "libx265" in encoders:
        disponiveis["cpu_h265"] = EncoderInfo("libx265", "cpu", "cpu")

    # GPU — testar de fato
    candidatos = [
        ("nvidia_h264", "h264_nvenc", "nvidia"),
        ("nvidia_h265", "hevc_nvenc", "nvidia"),
        ("intel_h264", "h264_qsv", "intel"),
        ("intel_h265", "hevc_qsv", "intel"),
        ("amd_h264", "h264_amf", "amd"),
        ("amd_h265", "hevc_amf", "amd"),
    ]
    for chave, codec, familia in candidatos:
        if codec in encoders and testar_encoder(codec):
            disponiveis[chave] = EncoderInfo(codec, "gpu", familia)

    return disponiveis


# =============================================================================
# Modelos de configuração
# =============================================================================

@dataclass
class Config:
    pasta_entrada: str = ""
    pasta_saida: str = ""
    recursivo: bool = True
    threshold: float = 27.0
    duracao_minima: float = 1.0
    ignorar_curtas: bool = True
    exibir_timecodes: bool = True
    organizar_subpastas: bool = True
    modo: str = MODO_AUTO
    encoder_pref: str = ENCODER_AUTO
    forcar_reencode: bool = False
    exportar_metadados: bool = True


# =============================================================================
# Núcleo de processamento
# =============================================================================

class Processador:
    """Processa lote de vídeos: detecta cenas e exporta cortes."""

    def __init__(
        self,
        config: Config,
        encoders: dict[str, EncoderInfo],
        log: Callable[[str], None],
        progresso: Callable[[float], None],
        cancelado: threading.Event,
    ) -> None:
        self.cfg = config
        self.encoders = encoders
        self.log = log
        self.progresso = progresso
        self.cancelado = cancelado

    # -------- descoberta de arquivos --------
    def coletar_videos(self) -> list[Path]:
        base = Path(self.cfg.pasta_entrada)
        if not base.exists():
            return []
        it = base.rglob("*") if self.cfg.recursivo else base.glob("*")
        return sorted([p for p in it if p.is_file() and p.suffix.lower() in VIDEO_EXTS])

    # -------- escolha de encoder --------
    def escolher_encoder(self) -> EncoderInfo:
        """Escolhe encoder conforme modo/preferência com fallback seguro."""
        cpu = self.encoders.get("cpu_h264") or EncoderInfo("libx264", "cpu", "cpu")

        def primeiro_gpu(familia: str) -> Optional[EncoderInfo]:
            for info in self.encoders.values():
                if info.tipo == "gpu" and info.familia == familia:
                    return info
            return None

        def qualquer_gpu() -> Optional[EncoderInfo]:
            for info in self.encoders.values():
                if info.tipo == "gpu":
                    return info
            return None

        pref = self.cfg.encoder_pref
        modo = self.cfg.modo

        if modo == MODO_QUALIDADE:
            return cpu

        if pref == ENCODER_CPU:
            return cpu
        if pref == ENCODER_NVIDIA:
            return primeiro_gpu("nvidia") or cpu
        if pref == ENCODER_INTEL:
            return primeiro_gpu("intel") or cpu
        if pref == ENCODER_AMD:
            return primeiro_gpu("amd") or cpu

        # Auto / Rápido
        gpu = qualquer_gpu()
        if modo == MODO_RAPIDO:
            return gpu or cpu
        # Automático: prefere GPU se houver, senão CPU
        return gpu or cpu

    # -------- detecção de cenas --------
    def detectar_cenas(self, video: Path) -> list[tuple[float, float]]:
        """Retorna lista de (inicio_seg, fim_seg)."""
        try:
            video_stream = open_video(str(video))
            sm = SceneManager()
            sm.add_detector(ContentDetector(threshold=self.cfg.threshold))
            sm.detect_scenes(video=video_stream, show_progress=False)
            lista = sm.get_scene_list()
        except Exception as exc:
            self.log(f"⚠️  Falha ao detectar cenas de {video.name}: {exc}")
            return []

        cenas: list[tuple[float, float]] = []
        for inicio, fim in lista:
            ini_s = inicio.get_seconds()
            fim_s = fim.get_seconds()
            if self.cfg.ignorar_curtas and (fim_s - ini_s) < self.cfg.duracao_minima:
                continue
            cenas.append((ini_s, fim_s))

        # Se PySceneDetect não retornou cenas, trata como cena única
        if not cenas:
            dur = self.duracao_video(video)
            if dur > 0:
                cenas = [(0.0, dur)]

        return cenas

    def duracao_video(self, video: Path) -> float:
        if not which_ffprobe():
            return 0.0
        rc, out, _ = run_cmd([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video)
        ])
        try:
            return float(out.strip()) if rc == 0 else 0.0
        except ValueError:
            return 0.0

    # -------- exportação --------
    def _params_qualidade(self, enc: EncoderInfo) -> list[str]:
        """Parâmetros de qualidade por encoder."""
        c = enc.codec
        if c == "libx264":
            return ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p"]
        if c == "libx265":
            return ["-c:v", "libx265", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p"]
        if c in ("h264_nvenc", "hevc_nvenc"):
            return ["-c:v", c, "-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
        if c in ("h264_qsv", "hevc_qsv"):
            return ["-c:v", c, "-preset", "slower", "-global_quality", "20"]
        if c in ("h264_amf", "hevc_amf"):
            return ["-c:v", c, "-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"]
        return ["-c:v", "libx264", "-preset", "slow", "-crf", "18"]

    def cortar_cena(
        self,
        video: Path,
        inicio: float,
        fim: float,
        destino: Path,
        enc: EncoderInfo,
    ) -> bool:
        """Corta cena. Tenta stream copy; faz fallback para reencode."""
        duracao = max(0.01, fim - inicio)

        # 1) Tentativa rápida com stream copy (se não for forçado reencode)
        if not self.cfg.forcar_reencode:
            cmd_copy = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{inicio:.3f}", "-i", str(video),
                "-t", f"{duracao:.3f}",
                "-c", "copy", "-avoid_negative_ts", "make_zero",
                str(destino),
            ]
            rc, _, err = run_cmd(cmd_copy)
            if rc == 0 and destino.exists() and destino.stat().st_size > 1024:
                # Verifica precisão: se stream copy gerou arquivo muito curto,
                # cai para reencode preciso.
                dur_saida = self.duracao_video(destino)
                if dur_saida >= duracao * 0.5:
                    return True
                self.log("   ↳ corte impreciso, tentando reencode…")

        # 2) Reencode preciso
        params = self._params_qualidade(enc)
        cmd_enc = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{inicio:.3f}", "-i", str(video),
            "-t", f"{duracao:.3f}",
            *params,
            "-c:a", "aac", "-b:a", "320k",
            "-movflags", "+faststart",
            str(destino),
        ]
        rc, _, err = run_cmd(cmd_enc)
        if rc == 0 and destino.exists():
            return True

        self.log(f"   ✗ Falha ao exportar: {err.strip().splitlines()[-1] if err.strip() else 'erro'}")

        # 3) Fallback final: CPU libx264 se estávamos em GPU
        if enc.tipo == "gpu":
            self.log("   ↳ fallback automático para CPU (libx264)…")
            cmd_cpu = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{inicio:.3f}", "-i", str(video),
                "-t", f"{duracao:.3f}",
                "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-c:a", "aac", "-b:a", "320k",
                "-movflags", "+faststart",
                str(destino),
            ]
            rc2, _, _ = run_cmd(cmd_cpu)
            return rc2 == 0 and destino.exists()

        return False

    # -------- loop principal --------
    def executar(self) -> None:
        videos = self.coletar_videos()
        if not videos:
            self.log("Nenhum vídeo encontrado na pasta de entrada.")
            self.progresso(1.0)
            return

        enc = self.escolher_encoder()
        self.log(f"🎬 {len(videos)} vídeo(s) encontrado(s).")
        self.log(f"⚙️  Encoder selecionado: {enc.codec} ({enc.tipo.upper()})")
        self.log(f"⚙️  Modo: {self.cfg.modo} | Threshold: {self.cfg.threshold}")

        saida_base = Path(self.cfg.pasta_saida)
        saida_base.mkdir(parents=True, exist_ok=True)

        total_cenas_globais = 0
        total_erros = 0

        for i, video in enumerate(videos, 1):
            if self.cancelado.is_set():
                self.log("⏹ Processamento cancelado pelo usuário.")
                break

            self.log("")
            self.log(f"[{i}/{len(videos)}] 🎞  {video.name}")

            try:
                cenas = self.detectar_cenas(video)
            except Exception as exc:
                self.log(f"   ✗ Erro na detecção: {exc}")
                total_erros += 1
                self.progresso(i / len(videos))
                continue

            if not cenas:
                self.log("   ⚠️  Nenhuma cena válida detectada — pulando.")
                self.progresso(i / len(videos))
                continue

            self.log(f"   ✓ {len(cenas)} cena(s) detectada(s).")

            # Pasta de destino
            nome_base = video.stem
            if self.cfg.organizar_subpastas:
                pasta_dest = saida_base / nome_base
            else:
                pasta_dest = saida_base
            pasta_dest.mkdir(parents=True, exist_ok=True)

            # Exportar metadados
            if self.cfg.exportar_metadados:
                self._exportar_metadados(pasta_dest, nome_base, cenas)

            # Exibir timecodes
            if self.cfg.exibir_timecodes:
                for idx, (a, b) in enumerate(cenas, 1):
                    self.log(f"      Cena {idx:03d}: {a:8.2f}s → {b:8.2f}s  ({b-a:.2f}s)")

            # Cortar cenas
            for idx, (ini, fim) in enumerate(cenas, 1):
                if self.cancelado.is_set():
                    break
                destino = pasta_dest / f"{nome_base}_Scene_{idx:03d}.mp4"
                self.log(f"   ▶ Exportando cena {idx}/{len(cenas)}…")
                try:
                    ok = self.cortar_cena(video, ini, fim, destino, enc)
                    if ok:
                        total_cenas_globais += 1
                    else:
                        total_erros += 1
                except Exception as exc:
                    total_erros += 1
                    self.log(f"   ✗ Exceção na cena {idx}: {exc}")

            self.progresso(i / len(videos))

        self.log("")
        self.log("=" * 60)
        self.log(f"✅ Concluído. Cenas exportadas: {total_cenas_globais} | Erros: {total_erros}")
        self.progresso(1.0)

    def _exportar_metadados(self, pasta: Path, nome_base: str, cenas: list[tuple[float, float]]) -> None:
        try:
            csv_path = pasta / f"{nome_base}_scenes.csv"
            json_path = pasta / f"{nome_base}_scenes.json"
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["scene", "start_sec", "end_sec", "duration_sec"])
                for i, (a, b) in enumerate(cenas, 1):
                    w.writerow([i, f"{a:.3f}", f"{b:.3f}", f"{b - a:.3f}"])
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(
                    [{"scene": i, "start": a, "end": b, "duration": b - a}
                     for i, (a, b) in enumerate(cenas, 1)],
                    f, indent=2, ensure_ascii=False,
                )
        except Exception as exc:
            self.log(f"   ⚠️  Não foi possível salvar metadados: {exc}")


# =============================================================================
# Interface Gráfica
# =============================================================================

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("green")


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Splitter Autocut 4K")
        self.geometry("1180x760")
        self.minsize(1000, 680)
        self._set_icone()

        self.cfg = Config()
        self.encoders: dict[str, EncoderInfo] = {}
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.thread: Optional[threading.Thread] = None
        self.cancelado = threading.Event()

        self._construir_ui()
        self.after(100, self._drenar_logs)
        self.after(200, self._pos_init)

    # -------- ícone da janela/taskbar --------
    def _set_icone(self) -> None:
        base = Path(__file__).resolve().parent
        try:
            if platform.system() == "Windows":
                self.iconbitmap(str(base / "icon.ico"))
            else:
                self._icone_img = PhotoImage(file=str(base / "icon.png"))
                self.iconphoto(True, self._icone_img)
        except Exception:
            pass

    # -------- construção da UI --------
    def _construir_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ---- Sidebar ----
        side = ctk.CTkScrollableFrame(self, width=340, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(side, text="✂️ Spliter Autocut 4k",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 4), padx=16, anchor="w")
        ctk.CTkLabel(side, text="Detecção e corte automático de cenas",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(padx=16, anchor="w")

        self.seg_tema = ctk.CTkSegmentedButton(side, values=["Claro", "Escuro"], command=self._on_tema)
        self.seg_tema.set("Claro")
        self.seg_tema.pack(padx=16, pady=(10, 0), fill="x")

        # --- Entrada ---
        ctk.CTkLabel(side, text="Pasta de Entrada", font=ctk.CTkFont(weight="bold")).pack(pady=(18, 2), padx=16, anchor="w")
        self.lbl_entrada = ctk.CTkLabel(side, text="(nenhuma selecionada)", wraplength=300, justify="left", text_color="gray")
        self.lbl_entrada.pack(padx=16, anchor="w")
        ctk.CTkButton(side, text="Selecionar entrada", command=self._pick_entrada).pack(pady=(4, 6), padx=16, fill="x")

        self.chk_recursivo = ctk.CTkCheckBox(side, text="Buscar recursivamente")
        self.chk_recursivo.select()
        self.chk_recursivo.pack(padx=16, anchor="w", pady=(0, 4))
        self.lbl_arquivos = ctk.CTkLabel(side, text="Arquivos detectados: 0", text_color="gray")
        self.lbl_arquivos.pack(padx=16, anchor="w")
        ctk.CTkButton(side, text="Escanear", height=26, command=self._escanear).pack(padx=16, fill="x", pady=(4, 0))

        # --- Saída ---
        ctk.CTkLabel(side, text="Pasta de Saída", font=ctk.CTkFont(weight="bold")).pack(pady=(14, 2), padx=16, anchor="w")
        self.lbl_saida = ctk.CTkLabel(side, text="(nenhuma selecionada)", wraplength=300, justify="left", text_color="gray")
        self.lbl_saida.pack(padx=16, anchor="w")
        ctk.CTkButton(side, text="Selecionar saída", command=self._pick_saida).pack(pady=(4, 6), padx=16, fill="x")
        self.chk_subpastas = ctk.CTkCheckBox(side, text="Organizar em subpastas")
        self.chk_subpastas.select()
        self.chk_subpastas.pack(padx=16, anchor="w")

        # --- Detecção ---
        ctk.CTkLabel(side, text="Detecção de Cenas", font=ctk.CTkFont(weight="bold")).pack(pady=(14, 2), padx=16, anchor="w")

        self.lbl_thr = ctk.CTkLabel(side, text="Sensibilidade (threshold): 27.0")
        self.lbl_thr.pack(padx=16, anchor="w")
        self.sld_thr = ctk.CTkSlider(side, from_=5, to=60, number_of_steps=110, command=self._on_thr)
        self.sld_thr.set(27.0)
        self.sld_thr.pack(padx=16, fill="x")

        ctk.CTkLabel(side, text="Duração mínima da cena (segundos):").pack(padx=16, anchor="w", pady=(6, 0))
        self.ent_min = ctk.CTkEntry(side)
        self.ent_min.insert(0, "1.0")
        self.ent_min.pack(padx=16, fill="x")

        self.chk_ignorar = ctk.CTkCheckBox(side, text="Ignorar cenas muito curtas")
        self.chk_ignorar.select()
        self.chk_ignorar.pack(padx=16, anchor="w", pady=(6, 0))

        self.chk_tc = ctk.CTkCheckBox(side, text="Exibir timecodes no log")
        self.chk_tc.select()
        self.chk_tc.pack(padx=16, anchor="w")

        self.chk_meta = ctk.CTkCheckBox(side, text="Gerar CSV/JSON de cenas")
        self.chk_meta.select()
        self.chk_meta.pack(padx=16, anchor="w")

        # --- Exportação ---
        ctk.CTkLabel(side, text="Exportação", font=ctk.CTkFont(weight="bold")).pack(pady=(14, 2), padx=16, anchor="w")
        ctk.CTkLabel(side, text="Modo:").pack(padx=16, anchor="w")
        self.cmb_modo = ctk.CTkComboBox(side, values=[MODO_AUTO, MODO_RAPIDO, MODO_QUALIDADE])
        self.cmb_modo.set(MODO_AUTO)
        self.cmb_modo.pack(padx=16, fill="x")

        ctk.CTkLabel(side, text="GPU/Encoder preferido:").pack(padx=16, anchor="w", pady=(6, 0))
        self.cmb_enc = ctk.CTkComboBox(side, values=[ENCODER_AUTO, ENCODER_CPU, ENCODER_NVIDIA, ENCODER_INTEL, ENCODER_AMD])
        self.cmb_enc.set(ENCODER_AUTO)
        self.cmb_enc.pack(padx=16, fill="x")

        self.chk_reenc = ctk.CTkCheckBox(side, text="Forçar reencode (mais preciso)")
        self.chk_reenc.pack(padx=16, anchor="w", pady=(6, 0))

        # --- Botões principais ---
        self.btn_iniciar = ctk.CTkButton(
            side, text="▶ INICIAR PROCESSAMENTO", height=48,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#16a34a", hover_color="#15803d",
            command=self._iniciar,
        )
        self.btn_iniciar.pack(padx=16, pady=(18, 6), fill="x")

        self.btn_cancelar = ctk.CTkButton(
            side, text="⏹ CANCELAR", height=36,
            fg_color="#b91c1c", hover_color="#7f1d1d",
            command=self._cancelar, state="disabled",
        )
        self.btn_cancelar.pack(padx=16, pady=(0, 6), fill="x")

        self.btn_abrir = ctk.CTkButton(side, text="📂 Abrir pasta de saída", command=self._abrir_saida)
        self.btn_abrir.pack(padx=16, pady=(0, 16), fill="x")

        # ---- Painel principal ----
        main = ctk.CTkFrame(self, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(main, height=64, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.lbl_status = ctk.CTkLabel(
            header, text="Aguardando…", font=ctk.CTkFont(size=14, weight="bold"), anchor="w"
        )
        self.lbl_status.grid(row=0, column=0, sticky="w", padx=16, pady=(10, 0))
        self.lbl_env = ctk.CTkLabel(header, text="", text_color="gray", anchor="w")
        self.lbl_env.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))

        self.txt_log = ctk.CTkTextbox(main, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)

        footer = ctk.CTkFrame(main, height=48, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.pb = ctk.CTkProgressBar(footer)
        self.pb.set(0)
        self.pb.grid(row=0, column=0, sticky="ew", padx=12, pady=12)

    # -------- pós-inicialização (checa ambiente) --------
    def _pos_init(self) -> None:
        self._log("=== Spliter Autocut 4k ===")
        self._log(f"Sistema: {platform.system()} {platform.release()}")

        if not which_ffmpeg() or not which_ffprobe():
            self._log("❌ FFmpeg/FFprobe NÃO encontrado no PATH.")
            self.lbl_env.configure(text="FFmpeg NÃO encontrado — instale antes de processar.")
            messagebox.showerror(
                "FFmpeg não encontrado",
                "FFmpeg e/ou FFprobe não estão instalados ou não estão no PATH.\n\n"
                "Windows: baixe em https://www.gyan.dev/ffmpeg/builds/ e adicione "
                "a pasta 'bin' ao PATH do sistema.\n\n"
                "macOS: brew install ffmpeg\n"
                "Linux: sudo apt install ffmpeg",
            )
            return

        self._log(f"✓ FFmpeg em: {which_ffmpeg()}")
        self._log(f"✓ FFprobe em: {which_ffprobe()}")
        self._log("Detectando encoders disponíveis…")

        def _detectar():
            self.encoders = detectar_encoders_disponiveis()
            gpu = [i.codec for i in self.encoders.values() if i.tipo == "gpu"]
            cpu = [i.codec for i in self.encoders.values() if i.tipo == "cpu"]
            self._log(f"✓ CPU: {', '.join(cpu) or 'nenhum'}")
            self._log(f"✓ GPU: {', '.join(gpu) or 'nenhuma detectada'}")
            env = f"FFmpeg OK · GPU: {', '.join(gpu) if gpu else 'não detectada (usará CPU)'}"
            self.after(0, lambda: self.lbl_env.configure(text=env))

        threading.Thread(target=_detectar, daemon=True).start()

    # -------- callbacks UI --------
    def _pick_entrada(self) -> None:
        p = filedialog.askdirectory(title="Selecione a pasta de entrada")
        if p:
            self.cfg.pasta_entrada = p
            self.lbl_entrada.configure(text=p, text_color="white")
            self._escanear()

    def _pick_saida(self) -> None:
        p = filedialog.askdirectory(title="Selecione a pasta de saída")
        if p:
            self.cfg.pasta_saida = p
            self.lbl_saida.configure(text=p, text_color="white")

    def _escanear(self) -> None:
        if not self.cfg.pasta_entrada:
            return
        self.cfg.recursivo = bool(self.chk_recursivo.get())
        base = Path(self.cfg.pasta_entrada)
        it = base.rglob("*") if self.cfg.recursivo else base.glob("*")
        arquivos = [p for p in it if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        self.lbl_arquivos.configure(text=f"Arquivos detectados: {len(arquivos)}")

    def _on_tema(self, valor: str) -> None:
        ctk.set_appearance_mode("dark" if valor == "Escuro" else "light")

    def _on_thr(self, v: float) -> None:
        self.lbl_thr.configure(text=f"Sensibilidade (threshold): {float(v):.1f}")

    def _abrir_saida(self) -> None:
        if not self.cfg.pasta_saida:
            messagebox.showinfo("Info", "Selecione uma pasta de saída primeiro.")
            return
        p = self.cfg.pasta_saida
        try:
            if platform.system() == "Windows":
                os.startfile(p)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception as exc:
            messagebox.showerror("Erro", str(exc))

    # -------- iniciar/cancelar --------
    def _coletar_config(self) -> bool:
        if not self.cfg.pasta_entrada or not Path(self.cfg.pasta_entrada).exists():
            messagebox.showerror("Erro", "Selecione uma pasta de entrada válida.")
            return False
        if not self.cfg.pasta_saida:
            messagebox.showerror("Erro", "Selecione uma pasta de saída.")
            return False

        try:
            Path(self.cfg.pasta_saida).mkdir(parents=True, exist_ok=True)
            # Testa permissão de escrita
            teste = Path(self.cfg.pasta_saida) / ".write_test.tmp"
            teste.write_text("ok")
            teste.unlink()
        except Exception as exc:
            messagebox.showerror("Erro", f"Sem permissão de escrita na pasta de saída:\n{exc}")
            return False

        if not which_ffmpeg() or not which_ffprobe():
            messagebox.showerror("Erro", "FFmpeg/FFprobe não estão disponíveis.")
            return False

        try:
            self.cfg.duracao_minima = max(0.0, float(self.ent_min.get().replace(",", ".")))
        except ValueError:
            self.cfg.duracao_minima = 1.0

        self.cfg.recursivo = bool(self.chk_recursivo.get())
        self.cfg.threshold = float(self.sld_thr.get())
        self.cfg.ignorar_curtas = bool(self.chk_ignorar.get())
        self.cfg.exibir_timecodes = bool(self.chk_tc.get())
        self.cfg.organizar_subpastas = bool(self.chk_subpastas.get())
        self.cfg.modo = self.cmb_modo.get()
        self.cfg.encoder_pref = self.cmb_enc.get()
        self.cfg.forcar_reencode = bool(self.chk_reenc.get())
        self.cfg.exportar_metadados = bool(self.chk_meta.get())
        return True

    def _iniciar(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        if not self._coletar_config():
            return

        self.cancelado.clear()
        self.pb.set(0)
        self.btn_iniciar.configure(state="disabled")
        self.btn_cancelar.configure(state="normal")
        self.lbl_status.configure(text="Processando…")

        proc = Processador(
            config=self.cfg,
            encoders=self.encoders,
            log=self._log,
            progresso=self._set_progress,
            cancelado=self.cancelado,
        )

        def _run():
            t0 = time.time()
            try:
                proc.executar()
            except Exception as exc:
                self._log(f"❌ Erro fatal: {exc}")
            finally:
                dt = time.time() - t0
                self._log(f"⏱ Tempo total: {dt:.1f}s")
                self.after(0, self._finalizar_ui)

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()

    def _cancelar(self) -> None:
        if self.thread and self.thread.is_alive():
            self.cancelado.set()
            self._log("⏹ Solicitação de cancelamento enviada…")

    def _finalizar_ui(self) -> None:
        self.btn_iniciar.configure(state="normal")
        self.btn_cancelar.configure(state="disabled")
        self.lbl_status.configure(text="Concluído.")

    # -------- log e progresso thread-safe --------
    def _log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _drenar_logs(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
        except queue.Empty:
            pass
        self.after(100, self._drenar_logs)

    def _set_progress(self, v: float) -> None:
        self.after(0, lambda: self.pb.set(max(0.0, min(1.0, v))))


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    try:
        app = App()
        app.mainloop()
    except Exception as exc:
        print("Erro fatal:", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
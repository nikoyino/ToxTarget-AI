# ToxTarget-AI: AI-Driven Toxicological Target Screening Platform

ToxTarget-AI is an intelligent analysis platform designed specifically for toxicology researchers. It combines the reasoning capabilities of Large Language Models (LLMs), biological databases (KEGG, UniProt, GTEx, HPA), and automated literature mining technologies to help researchers rapidly screen for functional targets most relevant to specific toxic phenotypes from a large volume of candidate proteins.

## Core Features

This platform offers two screening modes to meet research needs at different stages:

### 1. Quick Screening
Suitable for processing a large number of candidate proteins (tens to hundreds).
- **AI Functional Scoring**: Based on internal AI knowledge, scores the functional criticality of proteins regarding specific tissues and toxic phenotypes across multiple dimensions (functional relevance, tissue expression, pathway centrality).
- **Consensus Assessment**: Supports multi-round consistency evaluations to reduce AI hallucinations and provide more robust rankings.
- **Quick Report**: Generates a concise report containing rankings, scoring rationale, and core mechanistic hypotheses.

### 2. Detailed Screening
Suitable for in-depth validation of a small number of high-potential proteins (recommended 10-15).
- **Automated Literature Mining**: Automatically searches PubMed and Europe PMC to intelligently extract key evidence.
- **Multi-Database Integration**: Automatically queries KEGG pathways, UniProt functional annotations, and GTEx/HPA tissue expression data.
- **Virtual Expert Panel**: Simulates multi-round debates between a PI, Toxicologist, Cell Biologist, Scientific Critic, and other domain experts selected based on the toxic phenotype to not only rank proteins but also provide specific experimental validation plans.
- **Deep Review Generation**: Automatically writes a complete Mini-Review containing an executive summary, mechanism analysis, evidence evaluation, and references.

## Directory Structure

Before using, please ensure your folder structure looks like this:

```
ToxTarget-AI/                  # Project Root
│
├── start_app.py               # [Launcher] Double-click to run
│
├── README.md                  # Documentation
│
└── ToxTarget_Source/          # Source Code & Data Resources
    ├── main.py                # Main GUI Logic
    ├── core_engine.py         # Core Analysis Engine
    ├── services.py            # External Services (AI API, BioDB)
    ├── requirements.txt       # Dependency List
    ├── GTEx_Analysis_v8...    # Local Expression Database File
    └── rna_tissue_consensus...# Local HPA Database File
    └── ... (Other module files)
```

## Installation & Running

This project includes a fully automated deployment launcher, so you do not need to manually configure complex Python environments.

### Prerequisites
- **Operating System**: Windows 10/11, macOS, or Linux
- **Python**: Version 3.8 or higher

### Startup Steps

1. **Double-click** `start_app.py`
2. **Automatic Environment Configuration**:
   - On the first run, the launcher will automatically create an independent virtual environment named `venv` in the current directory
   - It will automatically detect and install all necessary dependencies (such as PyQt5, aiohttp, etc.)
   - **Optimized Experience**: The script includes multiple PyPI mirrors (Official, Tsinghua, Aliyun, Tencent) and will automatically switch to ensure fast installation in different network environments
3. **Enter Main Interface**: Once dependencies are installed, the program will automatically launch the main interface

> **Note**: The first run may take a few minutes to download dependencies; please be patient. Subsequent startups will be very fast.

## User Guide

### 1. Configure API Key
In the settings panel on the left side of the interface, enter your AI Model API Key (supports Gemini, DeepSeek, Qwen, etc.). It is recommended to click "Test API Connection" first.

### 2. Input Research Parameters
- **Pollutant**: E.g., "PFOA", "Cadmium"
- **Proteins**: Paste a list of protein symbols (e.g., ALB, CYP1A1), one per line or separated by commas
- **Target Tissue**: Select the tissue where toxicity occurs (e.g., Liver, Kidney)
- **Phenotypes**: E.g., "Steatosis", "Oxidative Stress"

### 3. Select Mode & Run
- Select **Quick Screening** or **Detailed Screening**
- Click **Start Screening** to begin the analysis

### 4. View Results
- During analysis, the right panel will display progress and logs in real-time
- After analysis, results can be exported in various formats including Excel spreadsheets, detailed Word reports, and Markdown notebooks

## Tech Stack

- **GUI**: PyQt5
- **Async Core**: asyncio, aiohttp
- **AI Integration**: Google Gemini API, OpenAI-compatible APIs
- **Data Parsing**: Pandas, BeautifulSoup4, LXML
- **Document Generation**: python-docx, openpyxl

## Disclaimer

This software is for scientific research assistance only. Although the system uses multiple verification mechanisms, AI-generated content (including literature summaries and mechanistic hypotheses) may still contain biases or errors. Please verify with original literature.

---

© 2025 ToxTarget-AI Project
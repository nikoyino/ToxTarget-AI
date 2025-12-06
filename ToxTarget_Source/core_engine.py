# -*- coding: utf-8 -*-
"""
Core Analysis Engine Module - Refactored Two-Stage Screening Version
"""

import asyncio
import logging
import json
import re
import math
import statistics
from typing import Dict, List, Optional, Tuple, Callable
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal, QThread

from services import BioDatabaseService, AIService
from utils import ConcurrencyManager, ExpressionAnalyzer, LiteratureManager, ResearchNotebook, sanitize_filename, DetailedWordExporter, DatabaseCSVExporter
from meeting_manager import ExpertSystemHelper

logger = logging.getLogger(__name__)

LANGUAGE_CONSTRAINT = "[All output in English]"

CORE_RESEARCH_PREMISE = """
[CRITICAL RESEARCH CONTEXT - READ FIRST]

This is a TARGET PROTEIN SCREENING study, NOT a binding validation study.

CONFIRMED FACTS (DO NOT question these):
✓ All candidate proteins have HIGH BINDING AFFINITY to {pollutant} (confirmed by molecular docking)
✓ We are NOT looking for evidence that binding occurs
✓ We are NOT validating docking results

RESEARCH QUESTION:
"Among proteins that bind to {pollutant}, which ones are FUNCTIONALLY CRITICAL
for the observed toxic phenotypes in {target_tissue}?"

FOCUS YOUR ANALYSIS ON:
1. What is this protein's normal function?
2. If binding disrupts/enhances this function, what cellular consequences occur?
3. How do these consequences relate to {target_phenotypes}?
4. Is this protein expressed in {target_tissue}?
5. Is this protein in critical pathways for {target_phenotypes}?

DO NOT waste effort on:
✗ Searching for binding evidence (already confirmed)
✗ Questioning if binding is real (it is)
✗ Asking for in vitro binding assays (that's for later)
"""

CORE_RESEARCH_PREMISE_POLLUTANT_AGNOSTIC = """
[CONTEXT] Functional criticality screening for {target_tissue} + {target_phenotypes}.
Rank proteins by impact if function disrupted.
Focus: Normal function → Disruption consequences → Phenotype link
Ignore: Pollutant specifics, binding mechanisms
"""

from quick_screener import QuickScreeningEngine

class TwoStageAnalysisEngine(QObject):
    """Orchestrates the two-stage (Quick/Detailed) analysis workflow."""

    log_signal = pyqtSignal(str, str)
    detailed_log_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(int, str, str)
    quick_screening_complete_signal = pyqtSignal(dict)
    literature_ready_signal = pyqtSignal(dict)
    analysis_complete_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.bio_service: Optional[BioDatabaseService] = None
        self.ai_client: Optional[AIService] = None
        self.smart_searcher: Optional['SmartLiteratureSearcher'] = None
        self.meeting_manager: Optional['ExpertMeetingManager'] = None
        self.fp_analyzer: Optional['FunctionPhenotypeAnalyzer'] = None
        self.expert_helper: Optional[ExpertSystemHelper] = None

        self.concurrency_mgr = ConcurrencyManager()
        self.expression_analyzer = ExpressionAnalyzer()
        self.notebook: Optional[ResearchNotebook] = None
        
        self.quick_screener = QuickScreeningEngine(parent_engine=self)

        self.is_running = False
        self.session_data: Dict = {}

    def emit_log(self, message: str, level: str = 'info'):
        self.log_signal.emit(message, level)

    def emit_detailed_log(self, title: str, content: str):
        self.detailed_log_signal.emit(title, content)
        logger.info(f"Detailed Log: {title} - Content length: {len(content)}")

    def emit_progress(self, percentage: int, main_text: str, detail_text: str = ''):
        self.progress_signal.emit(percentage, main_text, detail_text)

    def stop(self):
        self.is_running = False
        self.emit_log('Stop requested by user.', 'warn')

    def _initialize_services(self, inputs: Dict):
        self.emit_log('Initializing core services...', 'info')
        try:
            self.ai_client = AIService(
                model_name=inputs.get('model', 'gemini-1.5-flash'),
                api_key=inputs['api_key'],
                proxy=inputs.get('proxy')
            )
            self.emit_log(f"AI Service initialized (Model: {inputs.get('model')})", 'success')

            self.bio_service = BioDatabaseService(
                email=inputs['email'],
                proxy=inputs.get('proxy'),
                progress_callback=lambda msg: self.emit_log(f"DB: {msg}", 'info')
            )
            self.emit_log('BioDatabase Service initialized', 'success')

            from literature_services import SmartLiteratureSearcher
            self.smart_searcher = SmartLiteratureSearcher(
                bio_service=self.bio_service,
                ai_service=self.ai_client,
                rate_limiter=self.bio_service.rate_limiter,
                engine=self
            )
            self.emit_log('Smart Literature Searcher initialized', 'success')

            from meeting_manager import ExpertMeetingManager
            self.meeting_manager = ExpertMeetingManager(
                ai_service=self.ai_client,
                emit_log_callback=self.emit_log
            )
            self.emit_log('Expert Meeting Manager initialized', 'success')

            from function_phenotype_analyzer import FunctionPhenotypeAnalyzer
            self.fp_analyzer = FunctionPhenotypeAnalyzer()
            self.emit_log('Function-Phenotype Analyzer initialized', 'success')

            self.expert_helper = ExpertSystemHelper()
            self.emit_log('Expert System Helper initialized', 'success')

            self.emit_log('All services initialized successfully.', 'success')

        except Exception as e:
            logger.error(f"Service initialization failed: {e}", exc_info=True)
            self.emit_log(f'Error initializing services: {e}', 'error')
            raise

    async def quick_screening(self, inputs: Dict):
        try:
            self.is_running = True
            self.session_data = {'inputs': inputs, 'mode': 'quick'}
            
            try:
                if not hasattr(self, 'bio_service') or self.bio_service is None:
                    self.bio_service = BioDatabaseService(
                        email=inputs.get('email', 'quickscreen@example.com'),
                        proxy=inputs.get('proxy'),
                        progress_callback=lambda msg: self.emit_log(f"DB: {msg}", 'info')
                    )
                    self.emit_log('BioDatabase Service initialized for normalization', 'success')
                else:
                    self.emit_log('BioDatabase Service already initialized, reusing for normalization.', 'info')
            except Exception as e:
                logger.error(f"BioDatabaseService init failed in quick screening: {e}", exc_info=True)
                self.emit_log(f'Error initializing BioDatabaseService: {e}', 'error')
                self.error_signal.emit(f'BioDatabaseService init failed: {e}')
                return

            try:
                if not hasattr(self, 'ai_client') or self.ai_client is None:
                    self.ai_client = AIService(
                        model_name=inputs.get('model', 'gemini-1.5-flash'),
                        api_key=inputs['api_key'],
                        proxy=inputs.get('proxy')
                    )
                    self.emit_log(f"AI Service initialized for Quick Screening", 'success')
                else:
                    self.emit_log(f"AI Service already initialized, reusing.", 'info')
            except Exception as e:
                logger.error(f"AI Service initialization failed: {e}", exc_info=True)
                self.emit_log(f'Error initializing AI service: {e}', 'error')
                self.error_signal.emit(f'AI Service init failed: {e}')
                return

            self.quick_screener.set_services(self.ai_client, self.bio_service)
            await self.quick_screener.run_screening(inputs)

        except Exception as e:
            logger.error(f"Quick screening process failed: {e}", exc_info=True)
            self.error_signal.emit(f'Quick Screening Error: {str(e)}')
        finally:
            self.is_running = False

    async def detailed_screening(self, inputs: Dict):
        """Performs the full detailed analysis workflow."""
        try:
            self.is_running = True
            self.session_data = {'inputs': inputs, 'mode': 'detailed'}
            self.emit_log('Starting Detailed Screening Workflow...', 'info')
            self.emit_log(f'Target proteins: {len(inputs["proteins"])}', 'info')
            self.emit_progress(0, 'Initializing Detailed Screening', 'Setting up services')

            self._initialize_services(inputs)

            try:
                session_name = f"{sanitize_filename(inputs['pollutant'])}_{datetime.now().strftime('%Y%m%d')}"
                self.notebook = ResearchNotebook(session_name)
                self.session_data['notebook'] = self.notebook
                self.emit_log(f'Research Notebook "{session_name}" initialized.', 'success')
            except Exception as e:
                logger.error(f"Failed to initialize Research Notebook: {e}", exc_info=True)
                self.emit_log('Warning: Failed to initialize Research Notebook.', 'warn')
                self.notebook = None

            if not self.is_running: return
            self.emit_progress(3, 'Normalizing Protein Names', 'Standardizing identifiers')
            normalized_map = await self._normalize_protein_names(
                inputs['proteins'], inputs.get('species_taxon', 9606)
            )
            original_to_normalized = normalized_map
            inputs['proteins'] = list(dict.fromkeys(normalized_map.values()))
            inputs['protein_name_mapping'] = original_to_normalized
            self.session_data['inputs'] = inputs
            self.emit_log(f'Normalized {len(inputs["proteins"])} unique proteins.', 'success')

            if not self.is_running: return
            self.emit_progress(8, 'Fetching Tissue Expression', 'Querying GTEx/HPA (Local)')
            await self._fetch_tissue_expression(inputs)

            if not self.is_running: return
            self.emit_progress(12, 'Generating Research Hypothesis', 'AI contextualizing the study')
            await self._generate_hypothesis(inputs, use_pollutant_agnostic=False)
            if isinstance(self.session_data.get('hypothesis'), dict) and 'error' in self.session_data['hypothesis']:
                self.error_signal.emit(f'Hypothesis generation failed critically: {self.session_data["hypothesis"]["error"]}')
                return

            lit_manager = LiteratureManager()
            try:
                 collection_path = lit_manager.create_collection(self.session_data)
                 self.session_data['literature_collection_path'] = collection_path
                 self.emit_log(f"Literature collection created at: {collection_path}", 'info')
            except Exception as e:
                 logger.error(f"Failed to create literature collection: {e}", exc_info=True)
                 self.emit_log(f"Failed to create literature collection: {e}. Literature will not be saved to disk.", "error")

            if inputs.get('include_pollutant_background', True):
                if not self.is_running: return
                self.emit_progress(15, 'Pollutant Background Search', 'Gathering context literature')
                await self._search_pollutant_background(inputs)
            else:
                self.emit_log('Skipping pollutant background literature search.', 'info')
                self.session_data['pollutant_background_literature'] = {}

            if not self.is_running: return
            self.emit_progress(20, 'Stage 1 Literature Search', 'Finding protein function/interaction papers')
            stage1_literature = await self._search_stage1_literature(inputs)
            self.session_data['literature_stage1'] = stage1_literature

            self.literature_ready_signal.emit(stage1_literature)

        except Exception as e:
            logger.error(f"Detailed screening process failed: {e}", exc_info=True)
            self.error_signal.emit(f'Detailed Screening Error: {str(e)}')
            self.is_running = False

    async def continue_detailed_screening(self, selected_literature: Dict):
        """Resumes the detailed screening after user literature review."""
        try:
            if not self.is_running:
                 self.emit_log('Continue signal received, but analysis was already stopped.', 'warn')
                 self.is_running = False
                 return

            self.emit_log('Resuming Detailed Screening after literature review...', 'info')
            self.session_data['literature'] = {'selected': selected_literature}
            inputs = self.session_data['inputs']

            self.emit_log('Tissue expression data already loaded in Stage 1.', 'info')

            if not self.is_running: return
            self.emit_progress(60, 'Fetching Database Annotations', 'Querying KEGG/UniProt')
            await self._fetch_database_annotations(inputs)

            if not self.is_running: return
            self.emit_progress(75, 'Function-Phenotype Analysis', 'Integrating data streams')
            await self._run_function_phenotype_analysis(inputs)

            if not self.is_running: return
            self.emit_progress(85, 'Expert Review - Round 1', 'Simulating initial ranking')
            await self._run_expert_review_round1(inputs)

            if not self.is_running: return
            self.emit_progress(90, 'Stage 2 Literature Search', 'Addressing knowledge gaps')
            await self._search_stage2_literature(inputs)

            if not self.is_running: return
            self.emit_progress(95, 'Expert Review - Round 2', 'Final evaluation simulation')
            await self._run_expert_review_round2(inputs)

            if not self.is_running: return
            self.emit_progress(98, 'Generating Final Report', 'Synthesizing all findings')
            await self._generate_final_conclusion(inputs)

            if not self.is_running: return
            self.emit_progress(99, 'Generating Concise Report', 'AI summarizing final results')
            try:
                await self._generate_concise_detailed_report(inputs)
                self.emit_log('Concise detailed report generated.', 'success')
            except Exception as e:
                self.emit_log(f'Failed to generate concise detailed report: {e}', 'error')
                logger.error(f"Concise detailed report generation failed: {e}", exc_info=True)
                self.session_data['final_concise_report'] = f"# Report Generation Failed\n\nAn error occurred: {e}"

            if not self.is_running:
                 self.emit_log('Analysis stopped before final completion signal.', 'warn')
                 return

            self.emit_progress(100, 'Detailed Screening Complete', 'Final report generated.')
            self.emit_log('Detailed Screening workflow finished successfully.', 'success')
            self.analysis_complete_signal.emit(self.session_data)

        except Exception as e:
            logger.error(f"Continue detailed screening failed: {e}", exc_info=True)
            self.error_signal.emit(f'Continue Screening Error: {str(e)}')
        finally:
            self.is_running = False

    async def _normalize_protein_names(self, proteins: List[str], species_taxon: int) -> Dict[str, str]:
        """Normalizes protein names using BioDatabaseService with LLM-Assist."""
        if not self.bio_service:
            self.emit_log('BioDatabaseService not initialized, cannot normalize names.', 'error')
            return {p: p for p in proteins}

        self.emit_log(f'Normalizing {len(proteins)} protein names (Robust Mode)...', 'info')
        normalized_map = {}
        ortholog_map = {}
        processed_count = 0

        async def process_norm(protein):
            nonlocal processed_count
            if not self.is_running: return None
            try:
                if not self.bio_service:
                    raise RuntimeError("BioDatabaseService not available for normalization.")

                result = await self.bio_service.normalize_protein_name(
                    protein, 
                    species_taxon, 
                    self.ai_client 
                )
                
                processed_count += 1
                base_progress = 3 if self.session_data.get('mode') == 'detailed' else 2
                progress_range = 2 if self.session_data.get('mode') == 'detailed' else 3
                self.emit_progress(
                    base_progress + int((processed_count / len(proteins)) * progress_range),
                    'Normalizing Protein Names',
                    f'{processed_count}/{len(proteins)}: {protein}'
                )

                standard_name = None
                human_ortholog = None

                if result: 
                    standard_name = result['standard_name']
                    confidence = result.get('confidence', 'unknown')
                    log_level = 'success' if 'high' in confidence else 'warn'
                    
                    if protein.upper() != standard_name.upper():
                        self.emit_log(f'  {protein} -> {standard_name} (Confidence: {confidence})', log_level)
                    else:
                        self.emit_log(f'  {protein} -> {standard_name} (Verified)', 'success')
                    
                    if species_taxon != 9606:
                        try:
                            human_ortholog = await self.bio_service.get_human_ortholog(standard_name, species_taxon)
                            if human_ortholog:
                                self.emit_log(f'  {standard_name} -> Human ortholog: {human_ortholog}', 'info')
                        except Exception as orth_err:
                            logger.warning(f"Failed to get human ortholog for {standard_name}: {orth_err}")
                
                else:
                    self.emit_log(f'  {protein}: Normalization FAILED. Could not find a high-confidence match. This protein will be SKIPPED.', 'error')
                    standard_name = None
                    human_ortholog = None
                
                return protein, standard_name, human_ortholog
                
            except Exception as e:
                logger.error(f"Normalization failed for {protein}: {e}")
                self.emit_log(f'  {protein}: Error during normalization - {e}', 'error')
                processed_count += 1
                return protein, None, None

        results = await self.concurrency_mgr.process_batch(
            items=list(dict.fromkeys(proteins)),
            processor=process_norm,
            max_concurrent=1,
            timeout=45
        )

        for res in results:
            if res['success'] and res['result']:
                original, standard, human_orth = res['result']
                if standard: 
                    normalized_map[original] = standard
                    if human_orth:
                        ortholog_map[standard] = human_orth
            elif res['item']:
                pass

        original_set = set(proteins)
        for p in original_set:
            if p not in normalized_map:
                logger.debug(f'  {p}: Not included in final normalized map (failed validation).')

        self.session_data['ortholog_map'] = ortholog_map
        if ortholog_map:
            self.emit_log(f'Built ortholog map for {len(ortholog_map)} proteins.', 'success')
        
        self.emit_log(f'Normalization complete. Standardized {len(set(normalized_map.values()))} unique names.', 'success')
        return normalized_map

    async def _generate_hypothesis(self, inputs: Dict, use_pollutant_agnostic: bool = False):
        """Generates the initial research hypothesis using AI."""
        phenotypes_str = ', '.join(inputs.get('target_phenotypes', []))

        if use_pollutant_agnostic:
            premise = CORE_RESEARCH_PREMISE_POLLUTANT_AGNOSTIC.format(
                target_tissue=inputs['target_tissue'],
                target_phenotypes=phenotypes_str
            )
            hypothesis_task = f"Formulate a core hypothesis, background context, and key questions linking PROTEIN FUNCTIONAL DISRUPTION to the specific phenotypes of '{phenotypes_str}'."
            json_structure_guidance = f"""
[REQUIRED JSON OUTPUT]
Return a SINGLE JSON object (not an array):
{{
    "centralQuestion": "Restate the overarching central question, ensuring it mentions '{phenotypes_str}'.",
    "coreHypothesis": "Propose a 2-3 sentence core hypothesis linking protein functional disruption and specifically the '{phenotypes_str}' phenotypes.",
    "backgroundSummary": "Summarize why protein dysfunction in the target tissue could lead to '{phenotypes_str}'.",
    "keyQuestions": ["List 3-5 specific, testable questions derived from the hypothesis, focusing on '{phenotypes_str}'."]
}}
Do NOT wrap in array: [{{...}}] is WRONG."""
        else:
            premise = CORE_RESEARCH_PREMISE.format(
                pollutant=inputs['pollutant'],
                target_tissue=inputs['target_tissue'],
                target_phenotypes=phenotypes_str
            )
            hypothesis_task = "Formulate a core hypothesis, background context, and key questions based on the provided research premise."
            json_structure_guidance = """
[REQUIRED JSON OUTPUT]
Return a SINGLE JSON object (not an array):
{
    "centralQuestion": "Restate the overarching central question.",
    "coreHypothesis": "Propose a 2-3 sentence core hypothesis linking binding, function, and phenotype.",
    "backgroundSummary": "Summarize why protein dysfunction in the target tissue could lead to the phenotypes.",
    "keyQuestions": ["List 3-5 specific, testable questions derived from the hypothesis."]
}
Do NOT wrap in array: [{...}] is WRONG."""

        system_prompt_content = f"{premise}\n{LANGUAGE_CONSTRAINT}\n"
        
        user_prompt_content = f"""
[HYPOTHESIS GENERATION TASK]
{hypothesis_task}
"""
        if use_pollutant_agnostic:
            user_prompt_content += f"""
**CRITICAL INSTRUCTION: Your entire response (hypothesis, background, questions) MUST explicitly mention and focus on the specific target phenotypes: {phenotypes_str}. Do not replace them with generic terms like 'liver toxicity'.**
"""
        user_prompt_content += json_structure_guidance

        try:
            if not self.ai_client:
                 raise RuntimeError("AI Service not initialized for hypothesis generation.")
            
            response_raw = await self.ai_client.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=True, 
                temperature=0.0
            )

            response_dict = None

            if isinstance(response_raw, list):
                if len(response_raw) == 1 and isinstance(response_raw[0], dict):
                    response_dict = response_raw[0]
                else:
                    required_fields = {'coreHypothesis', 'centralQuestion'}
                    for item in response_raw:
                        if isinstance(item, dict) and any(field in item for field in required_fields):
                            response_dict = item
                            break
            elif isinstance(response_raw, dict):
                response_dict = response_raw

            if not response_dict:
                 raise TypeError(f"AI returned unexpected format: {type(response_raw)}")

            if response_dict:
                required_keys = ['coreHypothesis', 'centralQuestion', 'backgroundSummary', 'keyQuestions']
                missing = [k for k in required_keys if k not in response_dict]

                if missing:
                    defaults = {
                        'centralQuestion': f'What proteins are critical for {phenotypes_str}?',
                        'coreHypothesis': f'Protein disruption affects {phenotypes_str}',
                        'backgroundSummary': 'Protein function is crucial',
                        'keyQuestions': ['What are the consequences?']
                    }
                    for key in missing:
                        response_dict[key] = defaults.get(key, 'N/A')

            self.session_data['hypothesis'] = response_dict

            detailed_content = (
                f"Central Question: {response_dict.get('centralQuestion','N/A')}\n\n"
                f"Hypothesis: {response_dict.get('coreHypothesis','N/A')}\n\n"
                f"Background: {response_dict.get('backgroundSummary','N/A')}\n\n"
                f"Key Questions:\n- " + "\n- ".join(response_dict.get('keyQuestions',[]))
            )
            self.emit_detailed_log('Core Hypothesis Generated', detailed_content)
            self.emit_log('Core research hypothesis generated successfully.', 'success')

        except Exception as e:
            logger.error(f"Hypothesis generation failed: {e}", exc_info=True)
            self.emit_log(f'Hypothesis generation failed: {e}', 'warn')
            fallback_question = f"Which proteins are functionally critical for '{phenotypes_str}' in {inputs['target_tissue']} if disrupted?" if use_pollutant_agnostic else f"How does {inputs['pollutant']} binding affect protein function leading to toxicity?"
            fallback_hypothesis = f"Disruption of key proteins' functions in {inputs['target_tissue']} likely contributes to '{phenotypes_str}'." if use_pollutant_agnostic else f"{inputs['pollutant']} likely causes {inputs['target_tissue']} toxicity by disrupting functions of key binding proteins."
            self.session_data['hypothesis'] = {
                'centralQuestion': fallback_question,
                'coreHypothesis': fallback_hypothesis,
                'backgroundSummary': 'Protein function is crucial for tissue health.',
                'keyQuestions': ['What are the functional consequences of disruption?', 'Which pathways are most affected?'],
                'error': str(e)
            }

    async def _search_pollutant_background(self, inputs: Dict):
        """Searches for pollutant background literature."""
        self.emit_log('Searching pollutant background literature...', 'info')
        if not self.smart_searcher:
             self.emit_log('SmartLiteratureSearcher not initialized. Skipping pollutant background search.', 'error')
             self.session_data['pollutant_background_literature'] = {}
             return

        epmc_filters = {
            'open_access_only': inputs.get('open_access_only', False),
            'has_fulltext': inputs.get('prioritize_fulltext', True),
            'min_citations': inputs.get('min_citations', 0)
        }
        try:
            result = await self.smart_searcher.smart_search_pollutant_background(
                pollutant=inputs['pollutant'],
                target_tissue=inputs['target_tissue'],
                target_phenotypes=inputs.get('target_phenotypes', []),
                email=inputs['email'],
                limit=inputs.get('pollutant_lit_limit', 10),
                start_year=inputs.get('start_year', ''),
                end_year=inputs.get('end_year', ''),
                epmc_filters=epmc_filters
            )
            self.session_data['pollutant_background_literature'] = result
            collection_path = self.session_data.get('literature_collection_path')
            if collection_path:
                await self._save_literature_to_disk(
                    literature_data={'_pollutant_background': result},
                    collection_path=collection_path,
                    subfolder_prefix=""
                )
            else:
                 self.emit_log("Literature collection path not set, skipping background literature disk save.", "warn")

            if self.notebook:
                articles = result.get('articles', [])
                extracted_count = 0
                for article in articles:
                     extracted_info = article.get('extracted_info')
                     if extracted_info and isinstance(extracted_info, dict) and extracted_info.get('extraction_type'):
                          self.notebook.add_pollutant_background_note(extracted_info)
                          extracted_count += 1
                if extracted_count > 0:
                     self.emit_log(f"Added notes from {extracted_count} background articles to notebook.", "info")

            self.emit_log(f'Pollutant background search complete ({len(result.get("articles",[]))} articles found).', 'success')
        except Exception as e:
            logger.error(f"Pollutant background search failed: {e}", exc_info=True)
            self.emit_log(f'Pollutant background search failed: {e}', 'warn')
            self.session_data['pollutant_background_literature'] = {}

    async def _search_stage1_literature(self, inputs: Dict) -> Dict:
        """Performs Stage 1 literature search for all proteins."""
        if not self.smart_searcher:
             self.emit_log('SmartLiteratureSearcher not initialized. Skipping Stage 1 search.', 'error')
             return {}

        proteins = inputs['proteins']
        total_proteins = len(proteins)
        stage1_literature = {}
        processed_count = 0
        epmc_filters = {
            'open_access_only': inputs.get('open_access_only', False),
            'has_fulltext': inputs.get('fulltext_priority', True),
            'min_citations': inputs.get('min_citations', 0)
        }

        collection_path = self.session_data.get('literature_collection_path')

        for protein in proteins:
            if not self.is_running: break
            processed_count += 1
            self.emit_progress(
                20 + int((processed_count / total_proteins) * 35),
                f'Stage 1 Literature ({processed_count}/{total_proteins})',
                f'Searching for {protein}...'
            )
            try:
                result = await self.smart_searcher.smart_search_stage1(
                    protein=protein,
                    pollutant=inputs['pollutant'],
                    target_tissue=inputs['target_tissue'],
                    target_phenotypes=inputs.get('target_phenotypes', []),
                    email=inputs['email'],
                    limit=inputs.get('lit_limit', 15),
                    start_year=inputs.get('start_year', ''),
                    end_year=inputs.get('end_year', ''),
                    epmc_filters=epmc_filters
                )
                stage1_literature[protein] = result
                if collection_path:
                    await self._save_literature_to_disk(
                         literature_data={protein: result},
                         collection_path=collection_path,
                         subfolder_prefix="stage1_"
                    )
                if self.notebook:
                     articles = result.get('articles', [])
                     extracted_count = 0
                     for article in articles:
                          extracted_info = article.get('extracted_info')
                          if extracted_info and isinstance(extracted_info, dict) and extracted_info.get('extraction_type'):
                               self.notebook.add_protein_analysis_note(protein, extracted_info)
                               extracted_count += 1
                     if extracted_count > 0:
                          self.emit_log(f"Added notes from {extracted_count} articles for {protein} to notebook.", "debug")

            except Exception as e:
                logger.error(f"Stage 1 search failed for {protein}: {e}", exc_info=True)
                self.emit_log(f'Stage 1 search failed for {protein}: {e}', 'error')
                stage1_literature[protein] = {'articles': [], 'function_articles': [], 'interaction_articles': [], 'quality_metrics': {}}
            await asyncio.sleep(1.0)

        if collection_path:
             self._save_literature_metadata(collection_path, stage1_literature, inputs, "stage1")
        return stage1_literature

    async def _fetch_tissue_expression(self, inputs: Dict):
        """Fetches tissue expression data using local HPA and GTEx files."""
        if not self.bio_service:
            self.emit_log('BioDatabaseService not initialized. Skipping tissue expression fetch.', 'error')
            self.session_data['tissue_expression'] = {}
            return

        proteins = inputs['proteins']
        target_tissue = inputs['target_tissue']
        species_taxon = inputs.get('species_taxon', 9606)
        tissue_expression = {}
        processed_count = 0

        async def process_expr(protein):
            nonlocal processed_count
            if not self.is_running: return None
            result_data = None
            source_used = "N/A"
            
            query_gene_name = protein
            human_ortholog = self.session_data.get('ortholog_map', {}).get(protein)
            
            if species_taxon == 9606:
                query_gene_name = protein
                human_ortholog = None 
            elif human_ortholog:
                query_gene_name = human_ortholog
                self.emit_log(f"Using human ortholog {human_ortholog} for expression query of {protein}", 'info')
            else:
                self.emit_log(f"No human ortholog found for {protein}, skipping expression query.", 'warn')
                processed_count += 1
                self.emit_progress(55 + int((processed_count / len(proteins)) * 10), f'Fetching Expression ({processed_count}/{len(proteins)})', f'Skipped {protein}')
                return protein, None
            
            if query_gene_name:
                query_gene_name = query_gene_name.upper()
            else:
                self.emit_log(f"Cannot perform expression query for {protein}: No valid gene name.", 'warn')
                processed_count += 1
                self.emit_progress(55 + int((processed_count / len(proteins)) * 10), f'Fetching Expression ({processed_count}/{len(proteins)})', f'Skipped {protein}')
                return protein, None
            
            try:
                if not self.bio_service: raise RuntimeError("BioDatabaseService not available.")
                
                hpa_data = await self.bio_service.get_hpa_expression(query_gene_name)
                if hpa_data and hpa_data.get('rna_tissue_expression'):
                    tissue_expression_dict = hpa_data['rna_tissue_expression']
                    tissue_key_match = next((k for k in tissue_expression_dict.keys() if target_tissue.lower() in k.lower()), None)
                    if tissue_key_match:
                        ntpm_value = tissue_expression_dict.get(tissue_key_match, 0)
                        if ntpm_value is not None and ntpm_value > 0:
                            analysis = self.expression_analyzer.analyze_expression(ntpm_value, target_tissue)
                            result_data = {**analysis, 'source': 'HPA'}
                            source_used = "HPA"
                            msg = f"{protein}"
                            if human_ortholog:
                                msg += f" (via {human_ortholog})"
                            msg += f": Expression in {target_tissue} ({source_used}) - {analysis['expression_level']} ({analysis['tpm']} nTPM, {analysis['percentile']}% percentile)"
                            self.emit_log(msg, 'success')
                            if self.notebook: self.notebook.add_expression_data(protein, result_data)
                
                if not result_data:
                    gtex_data = await self.bio_service.get_gtex_expression(query_gene_name, target_tissue)
                    if gtex_data and gtex_data.get('median_tpm') is not None:
                        tpm_value = gtex_data['median_tpm']
                        if tpm_value > 0:
                            analysis = self.expression_analyzer.analyze_expression(tpm_value, target_tissue)
                            result_data = {**analysis, 'source': 'GTEx'}
                            source_used = "GTEx"
                            msg = f"{protein}"
                            if human_ortholog:
                                msg += f" (via {human_ortholog})"
                            msg += f": Expression in {target_tissue} ({source_used}) - {analysis['expression_level']} ({analysis['tpm']} TPM, {analysis['percentile']}% percentile)"
                            self.emit_log(msg, 'success')
                            if self.notebook: self.notebook.add_expression_data(protein, result_data)
                
                if not result_data:
                    self.emit_log(f"{protein} (Query: {query_gene_name}): No expression data found in {target_tissue} via local files.", 'warn')
                    if self.notebook: self.notebook.add_database_query_failure(protein, f"No expression data found in {target_tissue} via local files")
                    
            except Exception as e:
                logger.error(f"Expression query failed for {protein}: {e}")
                self.emit_log(f'Expression query error for {protein}: {e}', 'error')
                if self.notebook: self.notebook.add_database_query_failure(protein, f"Expression query failed: {e}")
                result_data = None
            finally:
                processed_count += 1
                self.emit_progress(55 + int((processed_count / len(proteins)) * 10), f'Fetching Expression ({processed_count}/{len(proteins)})', f'Queried local sources for {protein}')
            return protein, result_data

        results = await self.concurrency_mgr.process_batch(items=proteins, processor=process_expr, max_concurrent=2, timeout=60)
        for res in results:
            if res['success'] and res['result']:
                protein, expr_data = res['result']
                if expr_data: tissue_expression[protein] = expr_data
        self.session_data['tissue_expression'] = tissue_expression
        self.emit_log('Tissue expression analysis complete.', 'success')

    async def _fetch_database_annotations(self, inputs: Dict):
        """Fetches KEGG and UniProt annotations."""
        if not self.bio_service:
            self.emit_log('BioDatabaseService not initialized. Skipping DB annotations fetch.', 'error')
            self.session_data['database_analysis'] = {}
            return

        proteins = inputs['proteins']
        database_analysis = {}
        processed_count = 0
        total_proteins = len(proteins)

        async def process_annot(protein):
            nonlocal processed_count
            if not self.is_running: return None
            data = None
            try:
                if not self.bio_service: raise RuntimeError("BioDatabaseService not available.")
                
                human_ortholog = self.session_data.get('ortholog_map', {}).get(protein)
                
                species_data = await self.bio_service.query_comprehensive_data(
                    gene_symbol=protein,
                    species_taxon=inputs.get('species_taxon', 9606),
                    species_kegg=inputs.get('species_kegg', 'hsa'),
                    human_ortholog_override=human_ortholog
                )
                
                data = species_data

                if human_ortholog and inputs.get('species_taxon', 9606) != 9606 and human_ortholog.upper() != protein.upper():
                    self.emit_log(f"Querying conserved human data for {human_ortholog} (ortholog of {protein})", 'info')
                    human_data = await self.bio_service.query_comprehensive_data(
                        gene_symbol=human_ortholog,
                        species_taxon=9606,
                        species_kegg='hsa',
                        human_ortholog_override=None
                    )
                    
                    if human_data:
                        data = self._merge_comprehensive_data(species_data, human_data)
                
                if data:
                     avail = data.get('data_availability', {})
                     sources_found = sum(1 for k, found in avail.items() if found and 'human_' not in k)
                     human_sources_found = sum(1 for k, found in avail.items() if found and 'human_' in k)
                     
                     log_msg = f"{protein}: Found annotations in {sources_found} database(s)."
                     if human_sources_found > 0:
                         log_msg += f" Merged {human_sources_found} human ortholog DB(s)."
                     
                     if sources_found > 0 or human_sources_found > 0:
                          self.emit_log(log_msg, 'success')
                          if self.notebook: self.notebook.add_database_query_record(protein, data)
                     else:
                          self.emit_log(f"{protein}: No annotations found in KEGG or UniProt (species or human).", 'warn')
                          if self.notebook: self.notebook.add_database_query_failure(protein, "No KEGG/UniProt annotations found")
                          data = None
                else:
                     self.emit_log(f"{protein}: Comprehensive query returned no data.", 'warn')
                     if self.notebook: self.notebook.add_database_query_failure(protein, "Comprehensive DB query failed or returned empty")
                     data = None
            except Exception as e:
                logger.error(f"Database annotation failed for {protein}: {e}", exc_info=True)
                self.emit_log(f'Database annotation error for {protein}: {e}', 'error')
                if self.notebook: self.notebook.add_database_query_failure(protein, f"KEGG/UniProt query failed: {e}")
                data = None
            finally:
                processed_count += 1
                self.emit_progress(65 + int((processed_count / total_proteins) * 10), f'Fetching Annotations ({processed_count}/{total_proteins})', f'Queried DBs for {protein}')
            return protein, data

        results = await self.concurrency_mgr.process_batch(items=proteins, processor=process_annot, max_concurrent=2, timeout=90)
        for res in results:
            if res['success'] and res['result']:
                protein, annot_data = res['result']
                if annot_data: database_analysis[protein] = annot_data
        self.session_data['database_analysis'] = database_analysis
        self.emit_log('Database annotation fetching complete.', 'success')

    def _merge_comprehensive_data(self, species_data: Dict, human_data: Dict) -> Dict:
        """Smartly merges comprehensive data from species and human, prioritizing species data."""
        if not human_data:
            return species_data
        if not species_data:
            return human_data

        merged = species_data.copy()
        
        spec_func = merged.get('functional_data', {})
        hum_func = human_data.get('functional_data', {})

        spec_kegg = spec_func.get('kegg_pathways', {})
        hum_kegg = hum_func.get('kegg_pathways', {})
        spec_paths = {p.get('pathway_id'): p for p in spec_kegg.get('pathways', []) if p}
        hum_paths = {p.get('pathway_id'): p for p in hum_kegg.get('pathways', []) if p}
        spec_paths.update(hum_paths)
        spec_kegg['pathways'] = list(spec_paths.values())
        spec_kegg['pathway_count'] = len(spec_kegg['pathways'])

        spec_uniprot = spec_func.get('uniprot_functions', {})
        hum_uniprot = hum_func.get('uniprot_functions', {})
        spec_funcs = set(spec_uniprot.get('functions', []))
        spec_funcs.update(hum_uniprot.get('functions', []))
        spec_uniprot['functions'] = list(spec_funcs)

        merged.setdefault('data_availability', {})['human_kegg'] = human_data.get('data_availability', {}).get('kegg', False)
        merged['data_availability']['human_uniprot'] = human_data.get('data_availability', {}).get('uniprot', False)
        
        merged.setdefault('raw_data', {})['human_kegg'] = human_data.get('raw_data', {}).get('kegg')
        merged['raw_data']['human_uniprot'] = human_data.get('raw_data', {}).get('uniprot')

        self.emit_log(f"Merged human functional data into {merged.get('gene_symbol', 'protein')}", 'debug')
        return merged

    async def _run_function_phenotype_analysis(self, inputs: Dict):
        """Runs the objective function-phenotype analysis."""
        if not self.fp_analyzer:
             self.emit_log('FunctionPhenotypeAnalyzer not initialized. Skipping analysis.', 'error')
             self.session_data['function_phenotype_analysis'] = {}
             return

        proteins = inputs['proteins']
        fp_analysis_results = {}
        processed_count = 0
        total_proteins = len(proteins)

        async def process_fp(protein):
            nonlocal processed_count
            if not self.is_running: return None
            result_data = None
            try:
                db_data = self.session_data.get('database_analysis', {}).get(protein, {})
                selected_lit = self.session_data.get('literature', {}).get('selected', {})
                lit_data = selected_lit.get(protein, {})
                pheno_data = {}

                expr_data = self.session_data.get('tissue_expression', {}).get(protein, {})

                if not self.fp_analyzer: raise RuntimeError("FunctionPhenotypeAnalyzer not available.")
                result_data = self.fp_analyzer.analyze_function_phenotype_match(
                    protein=protein,
                    functional_data=db_data,
                    phenotype_data=pheno_data,
                    target_phenotypes=inputs.get('target_phenotypes', []),
                    literature_data=lit_data,
                    tissue_expression_data=expr_data
                )
                if result_data:
                     obs_count = len(result_data.get('match_observations', []))
                     gaps_count = len(result_data.get('knowledge_gaps', []))
                     self.emit_log(f"{protein}: Function-Phenotype analysis complete ({obs_count} obs, {gaps_count} gaps).", 'success')
            except Exception as e:
                logger.error(f"Function-Phenotype analysis failed for {protein}: {e}", exc_info=True)
                self.emit_log(f'Function-Phenotype analysis error for {protein}: {e}', 'error')
                result_data = None
            finally:
                 processed_count += 1
                 self.emit_progress(75 + int((processed_count / total_proteins) * 10), f'Function-Phenotype Analysis ({processed_count}/{total_proteins})', f'Analyzing {protein}...')
            return protein, result_data

        results = await self.concurrency_mgr.process_batch(items=proteins, processor=process_fp, max_concurrent=3, timeout=60)
        for res in results:
            if res['success'] and res['result']:
                protein, analysis_data = res['result']
                if analysis_data: fp_analysis_results[protein] = analysis_data
        self.session_data['function_phenotype_analysis'] = fp_analysis_results
        self.emit_log('Function-Phenotype analysis stage complete.', 'success')

        try:
            self.emit_log('Generating detailed database CSV export...', 'info')
            output_dir = Path('database_exports')
            output_dir.mkdir(exist_ok=True)
            safe_pollutant = sanitize_filename(inputs.get('pollutant', 'unknown'))
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            csv_filename = f"db_export_{safe_pollutant}_{timestamp}.csv"
            csv_path = output_dir / csv_filename
            
            exporter = DatabaseCSVExporter()
            exporter.export_database_csv(
                database_analysis=self.session_data.get('database_analysis', {}),
                tissue_expression=self.session_data.get('tissue_expression', {}),
                output_path=str(csv_path)
            )
            
            self.session_data['database_csv_path'] = str(csv_path)
            self.emit_log(f'Database CSV export saved to: {csv_path}', 'success')

        except Exception as e:
            logger.error(f"Failed to generate database CSV export: {e}", exc_info=True)
            self.emit_log(f'Failed to generate database CSV export: {e}', 'error')

    async def _run_expert_review_round1(self, inputs: Dict):
        """
        Runs the simulated expert review Round 1 (Ranking Flow).
        """
        if not self.meeting_manager or not self.expert_helper:
            self.emit_log('Meeting Manager or Expert Helper not initialized. Skipping Round 1.', 'error')
            self._set_default_round1_results()
            return

        fp_analysis = self.session_data.get('function_phenotype_analysis', {})
        valid_proteins = [
            p for p in inputs['proteins']
            if p in fp_analysis and isinstance(fp_analysis.get(p), dict) and fp_analysis[p].get('evidence_summary')
        ]

        if not valid_proteins:
            self.emit_log("No proteins have sufficient data for expert review. Skipping Round 1.", 'warn')
            self._set_default_round1_results()
            return

        self.emit_log(f'Expert Review Round 1 starting (Ranking Flow) for {len(valid_proteins)} proteins...', 'info')

        proteins_evidence_summaries = {}
        for protein in valid_proteins:
            if protein in fp_analysis and fp_analysis.get(protein):
                proteins_evidence_summaries[protein] = self.expert_helper.create_protein_evidence_summary(protein, fp_analysis[protein])
            else:
                proteins_evidence_summaries[protein] = f"=== {protein} Evidence Summary ===\nNo analysis data available.\n"
        
        context_data = {
            'hypothesis': self.session_data.get('hypothesis', {}),
            'proteins_evidence': proteins_evidence_summaries,
            'all_proteins_list': valid_proteins
        }

        try:
            if not self.meeting_manager: raise RuntimeError("Meeting Manager not available.")
            
            meeting_result = await self.meeting_manager.run_round1_discussion(
                experts=inputs['selected_experts'],
                context_data=context_data,
                pollutant=inputs['pollutant'],
                target_tissue=inputs['target_tissue'],
                target_phenotypes=inputs.get('target_phenotypes', []),
                valid_proteins=valid_proteins
            )
            
            self.session_data['round1_meeting'] = meeting_result

            pi_summary_ranking = meeting_result.get('pi_summary_ranking', [])
            critic_questions = meeting_result.get('critic_questions', [])
            
            self.session_data['round1_ranking'] = pi_summary_ranking
            self.session_data['round1_critiques'] = critic_questions
            self.session_data['round1_evidence_needs'] = critic_questions 
            self.session_data['round1_discussions'] = meeting_result.get('expert_rankings', {})

            if self.notebook:
                if critic_questions:
                    self.notebook.add_critical_questions(critic_questions, source="Expert Panel - R1 Critic")
                    self.emit_log(f"Recorded {len(critic_questions)} critical questions to notebook.", "debug")

            self.emit_log(f'Expert Review Round 1 finished. Preliminary ranking: {pi_summary_ranking}', 'success')

            history = meeting_result.get('discussion_history')
            if history:
                self._save_discussion_to_file(history, inputs, "round1")

        except Exception as e:
            logger.error(f"Expert Review Round 1 (Ranking Flow) failed: {e}", exc_info=True)
            self.emit_log(f'Expert Review Round 1 failed: {e}', 'error')
            self._set_default_round1_results()

    def _set_default_round1_results(self):
        """Sets default empty results for Round 1 if it fails or is skipped."""
        self.session_data['round1_meeting'] = {'discussion_history': [], 'final_summary': '', 'structured_results': {}}
        self.session_data['round1_discussions'] = []
        self.session_data['round1_critiques'] = []
        self.session_data['round1_evidence_needs'] = []
        self.session_data['round1_ranking'] = []

    async def _search_stage2_literature(self, inputs: Dict):
        """Performs Stage 2 targeted literature search."""
        if not self.smart_searcher:
             self.emit_log('SmartLiteratureSearcher not initialized. Skipping Stage 2 search.', 'error')
             self.session_data['literature_stage2'] = {p: {'articles': []} for p in inputs['proteins']}
             return

        critiques = self.session_data.get('round1_critiques', [])
        gaps = self.session_data.get('round1_evidence_needs', [])
        proteins_to_target = inputs['proteins']
        
        combined_gaps_critiques = list(set(critiques + gaps))

        if not combined_gaps_critiques:
            self.emit_log('No specific critiques or gaps identified from Round 1. Skipping Stage 2 search.', 'info')
            self.session_data['literature_stage2'] = {p: {'articles': []} for p in proteins_to_target}
            return

        stage2_literature = {}
        processed_count = 0
        total_to_process = len(proteins_to_target)
        epmc_filters = {
            'open_access_only': inputs.get('open_access_only', False),
            'has_fulltext': inputs.get('fulltext_priority', True),
            'min_citations': 0
        }
        collection_path = self.session_data.get('literature_collection_path')

        for protein in proteins_to_target:
            if not self.is_running: break
            processed_count += 1
            self.emit_progress(90 + int((processed_count / total_to_process) * 5), f'Stage 2 Literature ({processed_count}/{total_to_process})', f'Targeted search for {protein}...')

            protein_upper = protein.upper()
            protein_specific_gaps = [g for g in gaps if protein_upper in g.upper()]
            protein_specific_critiques = [c for c in critiques if protein_upper in c.upper()]

            if not protein_specific_gaps and not protein_specific_critiques:
                 self.emit_log(f"No specific gaps/critiques found to target for {protein}. Skipping Stage 2.", 'info')
                 stage2_literature[protein] = {'articles': []}
                 continue

            try:
                if not self.smart_searcher: raise RuntimeError("SmartLiteratureSearcher not available.")
                result = await self.smart_searcher.smart_search_stage2(
                    protein=protein, pollutant=inputs['pollutant'], target_tissue=inputs['target_tissue'],
                    knowledge_gaps=protein_specific_gaps, critique_points=protein_specific_critiques,
                    email=inputs['email'], limit=10, start_year=inputs.get('start_year', ''),
                    end_year=inputs.get('end_year', ''), epmc_filters=epmc_filters
                )
                stage2_literature[protein] = result
                if collection_path:
                     await self._save_literature_to_disk(literature_data={protein: result}, collection_path=collection_path, subfolder_prefix="stage2_")
                self.emit_log(f"{protein}: Stage 2 search found {len(result.get('articles', []))} articles.", 'success')
            except Exception as e:
                logger.error(f"Stage 2 search failed for {protein}: {e}", exc_info=True)
                self.emit_log(f'Stage 2 search failed for {protein}: {e}', 'error')
                stage2_literature[protein] = {'articles': []}
            await asyncio.sleep(0.5)

        if collection_path:
             self._save_literature_metadata(collection_path, stage2_literature, inputs, "stage2")
        self.session_data['literature_stage2'] = stage2_literature
        self.emit_log('Stage 2 targeted literature search complete.', 'success')

    async def _run_expert_review_round2(self, inputs: Dict):
        """Runs the simulated expert review Round 2 (Final Evaluation)."""
        if not self.meeting_manager or not self.expert_helper:
             self.emit_log('Meeting Manager or Expert Helper not initialized. Skipping Round 2.', 'error')
             self._set_default_round2_results()
             return

        round1_ranking = self.session_data.get('round1_ranking', [])
        critic_questions = self.session_data.get('round1_critiques', [])
        literature_stage2 = self.session_data.get('literature_stage2', {})
        fp_analysis = self.session_data.get('function_phenotype_analysis', {})

        if not round1_ranking:
            self.emit_log("No proteins were ranked in Round 1. Skipping Round 2.", 'warn')
            self._set_default_round2_results()
            return

        proteins_evidence_summaries = {}
        for protein in round1_ranking:
            if protein in fp_analysis and fp_analysis.get(protein):
                proteins_evidence_summaries[protein] = self.expert_helper.create_protein_evidence_summary(protein, fp_analysis[protein])
            else:
                proteins_evidence_summaries[protein] = f"=== {protein} Evidence Summary ===\nNo analysis data available.\n"

        proteins_summary_strings = []
        for protein in round1_ranking:
            summary = f"\n--- Evidence Summary for {protein} (for R2) ---\n"
            summary += f"Round 1 Rank: #{round1_ranking.index(protein) + 1}\n"
            
            if protein in fp_analysis and fp_analysis[protein]:
                 summary += self.expert_helper.create_protein_evidence_summary(protein, fp_analysis[protein])
            
            s2_lit = literature_stage2.get(protein, {})
            s2_articles = s2_lit.get('articles', [])
            if s2_articles:
                summary += f"\nStage 2 Findings ({len(s2_articles)} new articles):\n"
                resolutions = [art.get('gap_resolution', 'General finding') for art in s2_articles[:3]]
                summary += "  Key Resolutions:\n" + "\n".join([f"    - {res[:100]}..." for res in resolutions]) + "\n"
            else:
                summary += "\nStage 2 Findings: No new targeted literature found.\n"
            
            proteins_summary_strings.append(summary)

        context_data = {
            'hypothesis': self.session_data.get('hypothesis', {}),
            'round1_ranking': round1_ranking,
            'critic_questions': critic_questions,
            'literature_stage2': literature_stage2,
            'database_analysis_summary': self.session_data.get('database_analysis',{}),
            'tissue_expression_summary': self.session_data.get('tissue_expression',{}),
            'proteins_evidence': proteins_evidence_summaries 
        }

        try:
            if not self.meeting_manager: raise RuntimeError("Meeting Manager not available.")
            meeting_result = await self.meeting_manager.run_round2_discussion(
                experts=inputs['selected_experts'], context_data=context_data,
                pollutant=inputs['pollutant'], target_tissue=inputs['target_tissue'],
                target_phenotypes=inputs.get('target_phenotypes', []),
                proteins_summary=proteins_summary_strings
            )
            
            if self.notebook and meeting_result.get('final_summary'):
                self.notebook.update_synthesis_notes(f"**Round 2 - PI Final Summary**\n{meeting_result.get('final_summary')}")
                
            self.session_data['round2_meeting'] = meeting_result
            structured_results = meeting_result.get('structured_results', {})
            
            final_ranking = structured_results.get('final_ranking', [])
            research_plans = structured_results.get('research_plans', [])
            
            self.session_data['final_ranking'] = final_ranking
            self.session_data['research_plans'] = research_plans

            final_evals_proxy = []
            for i, protein in enumerate(final_ranking):
                plan = next((p['plan'] for p in research_plans if p['protein'] == protein), "N/A")
                final_evals_proxy.append({
                    "protein": protein,
                    "final_consensus_score": 100 - i, 
                    "revised_mechanistic_hypothesis": f"Ranked #{i+1} by PI.",
                    "experimental_validation_plan": [plan] if i < 3 else ["N/A"]
                })
            
            validated_evaluations = self._validate_final_evaluation_data(final_evals_proxy)
            self.session_data['round2_final_evaluations'] = validated_evaluations
            
            if self.notebook and validated_evaluations:
                self.emit_log("Recording final ranking insights to notebook...", "info")
                for evaluation in validated_evaluations[:5]: 
                    self.notebook.add_mechanistic_insight(
                        insight=f"[{evaluation.get('protein')}] {evaluation.get('revised_mechanistic_hypothesis')}",
                        supporting_pmids=[],
                        confidence=f"Final Rank: #{final_ranking.index(evaluation.get('protein')) + 1}"
                    )

            self.emit_log(f'Expert Review Round 2 finished. Final ranking: {final_ranking}', 'success')
            history = meeting_result.get('discussion_history')
            if history: self._save_discussion_to_file(history, inputs, "round2")

        except Exception as e:
            logger.error(f"Expert Review Round 2 failed: {e}", exc_info=True)
            self.emit_log(f'Expert Review Round 2 failed: {e}', 'error')
            self._set_default_round2_results()

    def _set_default_round2_results(self):
        """Sets default empty results for Round 2 if it fails or is skipped."""
        self.session_data['round2_meeting'] = {'discussion_history': [], 'final_summary': '', 'structured_results': {}}
        self.session_data['round2_final_evaluations'] = []
        self.session_data['final_ranking'] = []
        self.session_data['research_plans'] = []

    async def _generate_final_conclusion(self, inputs: Dict):
        """Generates the final conclusion/mini-review."""
        if inputs.get('deep_writing_mode', False):
            if not self.smart_searcher or not self.ai_client:
                 self.emit_log('Required services missing for Deep Review. Falling back to Standard.', 'error')
                 await self._standard_mini_review_generation()
            else:
                 try:
                      from deep_review_writer import DeepReviewWriter
                      self.deep_review_writer = DeepReviewWriter(ai_service=self.ai_client, emit_log_callback=self.emit_log)
                      await self._deep_research_mini_review_generation()
                 except ImportError:
                      self.emit_log('DeepReviewWriter module not found. Falling back to Standard Review.', 'error')
                      await self._standard_mini_review_generation()
                 except Exception as deep_e:
                      logger.error(f"Deep review generation failed critically: {deep_e}", exc_info=True)
                      self.emit_log(f'Deep review failed: {deep_e}. Attempting Standard Review fallback.', 'error')
                      await self._standard_mini_review_generation()
        else:
            await self._standard_mini_review_generation()

    async def _standard_mini_review_generation(self):
        """Generates the standard mini-review based on Round 2 evaluations with ENHANCED context."""
        self.emit_log('Generating Standard Mini-Review...', 'info')
        self.emit_progress(99, 'Generating Mini-Review', 'Synthesizing comprehensive report')
        
        if not self.ai_client:
             self.emit_log('AI Service not available for standard review generation.', 'error')
             self.session_data['final_evaluation'] = {'Mini-Review': {'error': 'AI Service unavailable'}, 'ranked_proteins': []}
             return

        inputs = self.session_data['inputs']
        hypothesis = self.session_data.get('hypothesis', {})
        
        # --- 1. Enhanced Pollutant Background Extraction ---
        bg_data = self.session_data.get('pollutant_background_literature', {})
        bg_summary_parts = []
        
        toxic_articles = bg_data.get('toxicology_articles', [])
        for art in toxic_articles[:5]: # Top 5 reviews
            if art.get('extracted_info'):
                ext = art['extracted_info']
                mechanisms = ", ".join(ext.get('molecular_mechanisms', []))
                effects = ", ".join(ext.get('toxic_effects', []))
                if mechanisms or effects:
                    bg_summary_parts.append(f"- [General Toxicity] (PMID:{art.get('pmid')}): Mechanisms include {mechanisms}. Effects include {effects}.")
        
        epi_articles = bg_data.get('epidemiology_articles', [])
        for art in epi_articles[:5]:
            if art.get('extracted_info'):
                ext = art['extracted_info']
                link = ", ".join(ext.get('functional_link_to_phenotype', []))
                if link:
                    bg_summary_parts.append(f"- [Phenotype Link] (PMID:{art.get('pmid')}): {link}")

        pollutant_context_str = "\n".join(bg_summary_parts) if bg_summary_parts else "No specific pollutant background extracted."

        # --- 2. Prepare Data Sources ---
        final_ranking = self.session_data.get('final_ranking', [])
        research_plans_from_pi = self.session_data.get('research_plans', [])
        db_analysis = self.session_data.get('database_analysis', {})
        tissue_expr = self.session_data.get('tissue_expression', {})
        selected_lit_s1 = self.session_data.get('literature', {}).get('selected', {})
        lit_s2 = self.session_data.get('literature_stage2', {})
        
        proteins_for_review = final_ranking[:10] 
        top_3_for_plans = final_ranking[:3]

        # --- 3. Build Citation-Rich Evidence Dossier ---
        evidence_dossier_parts = []
        
        if not proteins_for_review:
            evidence_dossier_parts.append("[EVIDENCE DOSSIER: NO PROTEINS RANKED]\nNo proteins were finalized.")
        else:
            evidence_dossier_parts.append(f"[EVIDENCE DOSSIER: Top {len(proteins_for_review)} Ranked Proteins]\n")
            
            for i, protein_name in enumerate(proteins_for_review):
                evidence_dossier_parts.append(f"\n### Rank #{i+1}: {protein_name}")
                
                # Database Facts
                protein_db = db_analysis.get(protein_name, {})
                if protein_db and protein_db.get('functional_data'):
                    kegg = protein_db['functional_data'].get('kegg_pathways', {})
                    uniprot = protein_db['functional_data'].get('uniprot_functions', {})
                    if kegg.get('pathways'):
                        paths = [p.get('pathway_name', 'N/A') for p in kegg['pathways'][:5]]
                        evidence_dossier_parts.append(f"  - **Database Fact (KEGG):** Involved in pathways: {'; '.join(paths)}.")
                    if uniprot.get('functions'):
                        funcs = uniprot['functions'][:3]
                        evidence_dossier_parts.append(f"  - **Database Fact (UniProt):** Functions: {'; '.join(funcs)}.")

                protein_expr = tissue_expr.get(protein_name, {})
                if protein_expr:
                    evidence_dossier_parts.append(f"  - **Expression Fact:** {protein_expr.get('expression_level', 'N/A')} in target tissue ({protein_expr.get('tpm', 'N/A')} TPM).")
                
                # Literature Evidence
                all_protein_articles = selected_lit_s1.get(protein_name, {}).get('articles', []) + \
                                       lit_s2.get(protein_name, {}).get('articles', [])
                
                valid_articles = [a for a in all_protein_articles if a.get('extracted_info')]
                seen_pmids = set()
                unique_articles = []
                for a in valid_articles:
                    if a.get('pmid') not in seen_pmids:
                        unique_articles.append(a)
                        seen_pmids.add(a.get('pmid'))

                if unique_articles:
                    evidence_dossier_parts.append("  - **Literature Evidence:**")
                    for art in unique_articles[:5]:
                        ext = art['extracted_info']
                        pmid = art.get('pmid')
                        key_point = ""
                        if ext.get('author_conclusions_on_phenotype'):
                            key_point = ext['author_conclusions_on_phenotype'][0]
                        elif ext.get('key_phenotype_results_data'):
                            key_point = ext['key_phenotype_results_data'][0]
                        elif ext.get('functional_link_to_phenotype'):
                            key_point = ext['functional_link_to_phenotype'][0]
                        
                        if key_point and key_point not in ["Not specified", "N/A"]:
                            evidence_dossier_parts.append(f"    * {key_point} (Source: PMID:{pmid})")
                else:
                    evidence_dossier_parts.append("  - **Literature Evidence:** No specific mechanistic details extracted.")

        evidence_dossier_str = "\n".join(evidence_dossier_parts)
        pi_plan_summary = "\n".join([f"PI Notes for {p.get('protein', 'N/A')}: {p.get('plan', 'N/A')}" for p in research_plans_from_pi if p.get('protein') in top_3_for_plans])

        # --- 4. Enhanced System Prompt ---
        system_prompt_content = f"""As a professional scientific editor, synthesize the provided information into a formal Mini-Review.
{LANGUAGE_CONSTRAINT}

[CRITICAL RULES FOR CITATION & ACCURACY]
1.  **NO HALLUCINATIONS:** You must ONLY use the information provided in the [POLLUTANT BACKGROUND] and [EVIDENCE DOSSIER]. Do not invent studies.
2.  **STRICT CITATION:** Whenever you make a claim derived from the Literature Evidence, you MUST cite the PMID provided in the input (e.g., "...activates the pathway (PMID:123456)"). 
3.  **INTEGRATION:** In the Introduction, explicitly use the [POLLUTANT BACKGROUND] to explain the general toxicity before narrowing down to the proteins.

[REQUIRED JSON OUTPUT FORMAT]
{{
  "executive_summary": "High-level summary of findings and top targets.",
  "introduction": "Background on {inputs['pollutant']} toxicity (cite PMIDs from Background), the problem statement, and the computational approach.",
  "top_protein_analysis": [ 
      {{"protein": "Name", "rank": 1, "analysis_text": "Detailed analysis citing PMIDs and Database facts..."}} 
  ],
  "integrated_mechanism": "How these proteins collectively explain the phenotype.",
  "conclusion_future_directions": "Summary and limitations.",
  "research_plans": [ 
      {{"protein": "Name", "plan": "Specific validation steps based on PI notes..."}} 
  ],
  "cited_pmids": ["List of PMIDs actually cited in the text"],
  "word_count": 0
}}"""

        user_prompt_content = f"""
[ANALYSIS CONTEXT]
Pollutant: {inputs['pollutant']}
Tissue: {inputs['target_tissue']}
Phenotypes: {', '.join(inputs.get('target_phenotypes',[]))}
Core Hypothesis: {hypothesis.get('coreHypothesis', 'N/A')}

[POLLUTANT BACKGROUND (Use for Introduction)]
{pollutant_context_str}

[PI NOTES ON RESEARCH PLANS]
{pi_plan_summary}

[EVIDENCE DOSSIER (Detailed Protein Data)]
{evidence_dossier_str}

[TASK]
Write the mini-review. Ensure the 'Introduction' utilizes the Pollutant Background citations, and the 'Top Protein Analysis' utilizes the Literature Evidence citations.
"""
        
        try:
            response = await self.ai_client.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=True, 
                temperature=0.2 
            )
            
            if not isinstance(response, dict) or not response.get('executive_summary'):
                raise ValueError("AI response did not follow the required JSON structure.")
            if 'word_count' not in response:
                 response['word_count'] = sum(len(str(v).split()) for v in response.values() if isinstance(v, str))
            
            ranked_proteins_list_to_save = []
            for i, protein_name in enumerate(final_ranking):
                ranked_proteins_list_to_save.append({
                    "protein": protein_name,
                    "rank": i + 1,
                    "final_consensus_score": 100 - (i * (100 / max(1, len(final_ranking))))
                })
            
            self.session_data['final_evaluation'] = {
                'Mini-Review': response, 
                'ranked_proteins': ranked_proteins_list_to_save
            }
            
            self._save_conclusion_to_file(response, inputs)
            self.emit_log('Standard mini-review generated successfully with enhanced citations.', 'success')

        except Exception as e:
            logger.error(f"Standard mini-review generation failed: {e}", exc_info=True)
            self.emit_log(f'Standard mini-review generation failed: {e}', 'error')
            self.session_data['final_evaluation'] = {
                'Mini-Review': {'error': str(e)}, 
                'ranked_proteins': []
            }

    async def _deep_research_mini_review_generation(self):
        """
        Generates the deep research mini-review by orchestrating the DeepReviewWriter.
        """
        self.emit_log('Generating Deep Research Mini-Review...', 'info')
        self.emit_progress(99, 'Writing Mini-Review', 'Deep analysis & writing (may take 5-8 mins)')

        if not hasattr(self, 'deep_review_writer') or not self.deep_review_writer:
            self.emit_log('DeepReviewWriter not initialized.', 'error')
            
            self.session_data['final_evaluation'] = {
                'Mini-Review': {'error': 'DeepReviewWriter missing'},
                'ranked_proteins': []
            }
            return

        inputs = self.session_data['inputs']
        expert_evals = self.session_data.get('round2_final_evaluations', [])
        stage1_lit_selected = self.session_data.get('literature', {}).get('selected', {})
        stage2_lit = self.session_data.get('literature_stage2', {})

        combined_lit_for_deep_extract = {}
        proteins_reviewed_r2 = {ev.get('protein') for ev in expert_evals if ev and ev.get('protein')}

        for protein in proteins_reviewed_r2:
            s1_data = stage1_lit_selected.get(protein, {})
            s2_data = stage2_lit.get(protein, {})
            s1_articles = s1_data.get('articles', []) if isinstance(s1_data, dict) else []
            s2_articles = s2_data.get('articles', []) if isinstance(s2_data, dict) else []
            s1_articles = s1_articles if isinstance(s1_articles, list) else []
            s2_articles = s2_articles if isinstance(s2_articles, list) else []

            merged_articles_dict = {
                self.bio_service.literature_coordinator._get_article_key(a): a
                for a in s1_articles + s2_articles
                if isinstance(a, dict) and self.bio_service.literature_coordinator._get_article_key(a)
            }

            combined_data = s1_data.copy() if s1_data else {}
            combined_data.update(s2_data if s2_data else {})
            combined_data['articles'] = list(merged_articles_dict.values())
            combined_lit_for_deep_extract[protein] = combined_data

        ranked_proteins = sorted(
            [ev for ev in expert_evals if ev and ev.get('protein') in proteins_reviewed_r2],
            key=lambda x: x.get('final_consensus_score', 0),
            reverse=True
        )
        top_proteins_for_deep = ranked_proteins[:5]

        if not top_proteins_for_deep:
            self.emit_log("No proteins available for deep review generation.", 'warn')
            
            self.session_data['final_evaluation'] = {
                'Mini-Review': {'error': 'No proteins eligible for deep review'},
                'ranked_proteins': []
            }
            return

        writer = self.deep_review_writer
        writer.register_valid_pmids(combined_lit_for_deep_extract)

        # --- Pollutant Background Extraction ---
        bg_data = self.session_data.get('pollutant_background_literature', {})
        bg_summary_parts = []
        
        all_bg_articles = bg_data.get('articles', [])
        for art in all_bg_articles:
            if art.get('pmid'):
                writer.valid_pmids.add(str(art['pmid']))

        toxic_articles = bg_data.get('toxicology_articles', [])
        for art in toxic_articles[:5]:
            if art.get('extracted_info'):
                ext = art['extracted_info']
                mechanisms = ", ".join(ext.get('molecular_mechanisms', []))
                if mechanisms:
                    bg_summary_parts.append(f"- [General Toxicity] {mechanisms} (PMID:{art.get('pmid')})")
        
        epi_articles = bg_data.get('epidemiology_articles', [])
        for art in epi_articles[:5]:
            if art.get('extracted_info'):
                ext = art['extracted_info']
                link = ", ".join(ext.get('functional_link_to_phenotype', []))
                if link:
                    bg_summary_parts.append(f"- [Phenotype Link] {link} (PMID:{art.get('pmid')})")

        pollutant_context_str = "\n".join(bg_summary_parts) if bg_summary_parts else "No specific pollutant background extracted."
        self.emit_log(f"Deep Review: Injected {len(bg_summary_parts)} background context points.", 'info')

        try:
            critical_extracts = await self.smart_searcher.deep_extract_critical_papers(
                all_literature=combined_lit_for_deep_extract,
                top_proteins=[p['protein'] for p in top_proteins_for_deep],
                max_papers=15
            )
            for pmid in critical_extracts.keys():
                writer.valid_pmids.add(str(pmid))
            if not self.is_running: return

            gaps = await self.smart_searcher.identify_knowledge_gaps(
                top_proteins=top_proteins_for_deep,
                existing_literature=combined_lit_for_deep_extract,
                pollutant=inputs['pollutant'],
                target_phenotypes=inputs.get('target_phenotypes', [])
            )
            if not self.is_running: return

            mechanistic_network_input = {
                'pollutant_background_summary': self.session_data.get('pollutant_background_literature', {}).get('summary', ''),
                'top_protein_mechanisms': {
                    p.get('protein'): p.get('revised_mechanistic_hypothesis', 'N/A') for p in top_proteins_for_deep
                }
            }
            if not self.is_running: return

            db_analysis_raw = self.session_data.get('database_analysis', {})
            
            expert_summary = self.session_data.get('round2_meeting', {}).get('final_summary', '')
            critic_qs = "\n".join(self.session_data.get('round1_critiques', []))
            expert_context = f"Pollutant Background Context:\n{pollutant_context_str}\n\nPI Final Summary:\n{expert_summary}\n\nUnresolved Critic Questions:\n{critic_qs}"

            outline = await writer.create_detailed_outline(
                top_proteins=top_proteins_for_deep,
                critical_extracts=critical_extracts,
                mechanistic_network=mechanistic_network_input,
                inputs=inputs,
                expert_consensus_summary=expert_context, 
                database_facts=db_analysis_raw           
            )
            if not self.is_running: return

            full_dossier = writer._prepare_evidence_summary(
                critical_extracts, 
                [p.get('protein') for p in top_proteins_for_deep],
                database_info=db_analysis_raw,
                expert_opinions=expert_context,
                pollutant_context=pollutant_context_str
            )

            sections = {}
            section_names = list(outline.keys())
            total_sections = len(section_names)
            for idx, section_name in enumerate(section_names):
                if not self.is_running: return
                self.emit_progress(99, f'Writing Section {idx+1}/{total_sections}', section_name.replace("_", " ").title())
                if section_name in outline:
                    context_data = {
                        'pollutant': inputs.get('pollutant'),
                        'tissue': inputs.get('target_tissue'),
                        'proteins': [p.get('protein') for p in top_proteins_for_deep if p.get('protein')],
                        'pollutant_context': pollutant_context_str
                    }
                    sections[section_name] = await writer.write_section(
                        section_name=section_name,
                        outline_section=outline.get(section_name, {}),
                        critical_extracts=critical_extracts,
                        context_data=context_data,
                        full_evidence_dossier=full_dossier 
                    )
                await asyncio.sleep(0.2)

            if not self.is_running: return

            integrated = await writer.integrate_sections(sections, outline)
            if not self.is_running: return
            review = await writer.critical_review(integrated)
            if not self.is_running: return
            # final_text = await writer.apply_improvements(integrated, review, full_evidence_dossier=full_dossier)
            final_text = integrated
            self.emit_log('Skipped "Apply Improvements" step to prevent text truncation.', 'info')
            
            final_ranking_names = self.session_data.get('final_ranking', [])
            top_3_proteins = final_ranking_names[:3]
            research_plans_from_pi = self.session_data.get('research_plans', [])
            pi_plan_summary = "\n".join([f"PI Notes for {p.get('protein', 'N/A')}: {p.get('plan', 'N/A')}" for p in research_plans_from_pi if p.get('protein') in top_3_proteins])

            generated_plans = []
            if top_3_proteins:
                self.emit_log('Generating research plans for Top 3 proteins...', 'info')
                plan_system_prompt = f"""You are a PI. Based on a deep analysis, generate specific research plans.
{LANGUAGE_CONSTRAINT}
[Required JSON Output Format]
Return ONLY a JSON object with a single key "research_plans".
{{
  "research_plans": [
    {{"protein": "Top1_Name", "plan": "Detailed, specific, step-by-step research plan..."}},
    {{"protein": "Top2_Name", "plan": "..."}},
    {{"protein": "Top3_Name", "plan": "..."}}
  ]
}}"""
                plan_user_prompt = f"""
[Top 3 Proteins]
{', '.join(top_3_proteins)}

[PI's Initial Notes]
{pi_plan_summary}

[Full Review Text (for context)]
{final_text[:5000]}... 

[TASK]
Generate detailed, specific, and actionable research plans for the Top 3 proteins listed. Use the PI's notes and the full review text for context.
"""
                try:
                    research_plans_json = await self.ai_client.generate(
                        system_prompt=plan_system_prompt,
                        user_prompt=plan_user_prompt,
                        is_json=True,
                        temperature=0.3
                    )
                    generated_plans = research_plans_json.get('research_plans', [])
                    self.emit_log(f"Generated {len(generated_plans)} research plans.", 'success')
                except Exception as plan_e:
                    self.emit_log(f"Failed to generate research plans: {plan_e}", 'error')
                    generated_plans = [{"protein": p, "plan": "Error generating plan."} for p in top_3_proteins]

            cited_pmids_raw = re.findall(r'PMID\s*(\d{7,8})', final_text)
            cited_pmids = list(set(pmid for pmid in cited_pmids_raw if pmid in writer.valid_pmids))

            final_conclusion = {
                'executive_summary': sections.get('executive_summary', '[Not generated]'),
                'introduction': sections.get('introduction', '[Not generated]'),
                'top_protein_analysis': self._format_protein_analysis_deep(sections.get('protein_analysis', '[Not generated]'), top_proteins_for_deep),
                'integrated_mechanism': sections.get('mechanistic_model', '[Not generated]'),
                'evidence_evaluation': sections.get('evidence_evaluation', '[Not generated]'),
                'validation_strategy': sections.get('validation_strategy', '[Not generated]'),
                'clinical_implications': sections.get('clinical_implications', '[Not generated]'),
                'conclusions': sections.get('conclusions', '[Not generated]'),
                'research_plans': generated_plans, 
                'cited_pmids': cited_pmids,
                'word_count': len(final_text.split()),
                'quality_assessment': review.get('overall_quality', 'N/A'),
                'deep_analysis_metadata': {
                    'critical_papers_analyzed': len(critical_extracts),
                    'knowledge_gaps_identified': len(gaps),
                    'total_citations': len(cited_pmids)
                }
            }
            
            self.session_data['final_evaluation'] = {
                'hypothesis': self.session_data.get('hypothesis', {}),
                'Mini-Review': final_conclusion, 
                'ranked_proteins': top_proteins_for_deep,
                'full_review_text': final_text,
                'critical_review': review
            }

            self._display_conclusion_index(final_conclusion)
            self._save_conclusion_to_file(final_conclusion, inputs)
            self._save_full_review_text(final_text, inputs)

            self.emit_log('Deep research mini-review generation complete.', 'success')

        except Exception as e:
            logger.error(f"Deep review generation process failed: {e}", exc_info=True)
            self.emit_log(f'Deep review generation process failed: {e}. Check logs.', 'error')
            
            self.session_data['final_evaluation'] = {
                'Mini-Review': {'error': f'Deep review failed: {e}'},
                'ranked_proteins': top_proteins_for_deep
            }

    def _validate_discussion_data(self, discussions: List[Dict]) -> List[Dict]:
        """Validates the structure of discussion results."""
        validated = []
        if not isinstance(discussions, list): logger.warning("Discussion results not a list."); return []
        for item in discussions:
            if not isinstance(item, dict): continue
            cleaned_item = item.copy()
            for key in ['strengths', 'weaknesses', 'critic_challenges', 'evidence_needs']:
                value = cleaned_item.get(key)
                if isinstance(value, str): cleaned_item[key] = [value] if value.strip() else []
                elif not isinstance(value, list): cleaned_item[key] = []
                else: cleaned_item[key] = [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
            validated.append(cleaned_item)
        return validated

    def _validate_final_evaluation_data(self, evaluations: List[Dict]) -> List[Dict]:
        """Validates the structure of final evaluation results."""
        validated = []
        if not isinstance(evaluations, list): logger.warning("Final evaluation results not a list."); return []
        for item in evaluations:
            if not isinstance(item, dict): continue
            cleaned_item = item.copy()
            plan = cleaned_item.get('experimental_validation_plan')
            if isinstance(plan, str): cleaned_item['experimental_validation_plan'] = [plan] if plan.strip() else []
            elif not isinstance(plan, list): cleaned_item['experimental_validation_plan'] = []
            else: cleaned_item['experimental_validation_plan'] = [str(p).strip() for p in plan if isinstance(p, (str, int, float)) and str(p).strip()]
            score = cleaned_item.get('final_consensus_score', cleaned_item.get('consensus_score'))
            try: cleaned_item['final_consensus_score'] = float(score) if score is not None else 0.0
            except (ValueError, TypeError): cleaned_item['final_consensus_score'] = 0.0; logger.warning(f"Invalid score for {cleaned_item.get('protein')}: {score}")
            validated.append(cleaned_item)
        return validated

    def _save_article_abstract(self, article: Dict, base_path: str, subfolder: str):
        """Save a single article's abstract to a .txt file."""
        if not isinstance(article, dict): return
        pmid = article.get('pmid', 'unknown_pmid'); title = article.get('title', 'untitled_article')
        safe_title = sanitize_filename(title)[:50]; filename = f"{pmid}_{safe_title}.txt"
        save_dir = Path(base_path) / 'abstracts' / sanitize_filename(subfolder); save_dir.mkdir(parents=True, exist_ok=True)
        filepath = save_dir / filename
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"PMID: {pmid}\nTitle: {article.get('title', 'N/A')}\nYear: {article.get('year', 'N/A')}\n")
                score = article.get('ai_relevance_score'); f.write(f"AI Relevance Score: {score:.1f}/10\n" if score is not None else "")
                f.write(f"Source: {article.get('source', 'N/A')}\n" + "-" * 60 + "\n\n" + article.get('abstract', 'No abstract available.'))
                reason = article.get('ai_relevance_reason'); f.write("\n\n" + "-"*30 + "\nAI Rationale:\n" + reason if reason else "")
        except Exception as e: logger.error(f"Failed to save abstract {pmid} to {filepath}: {e}")

    def _save_article_fulltext(self, article: Dict, base_path: str, subfolder: str):
        """Save the full text of an article to a .txt file."""
        if not isinstance(article, dict) or not article.get('full_text'): return
        pmid = article.get('pmid', 'unknown_pmid'); title = article.get('title', 'untitled_article')
        safe_title = sanitize_filename(title)[:50]; filename = f"{pmid}_{safe_title}_FULLTEXT.txt"
        save_dir = Path(base_path) / 'fulltext' / sanitize_filename(subfolder); save_dir.mkdir(parents=True, exist_ok=True)
        filepath = save_dir / filename
        try:
            with open(filepath, 'w', encoding='utf-8') as f: f.write(article['full_text'])
        except Exception as e: logger.error(f"Failed to save fulltext {pmid} to {filepath}: {e}")

    def _save_article_pdf(self, article: Dict, pdf_binary: bytes, base_path: str, subfolder: str):
        """Save the raw PDF file."""
        if not isinstance(article, dict) or not pdf_binary: return
        pmid = article.get('pmid', 'unknown_pmid'); title = article.get('title', 'untitled_article')
        safe_title = sanitize_filename(title)[:50]; filename = f"{pmid}_{safe_title}.pdf"
        save_dir = Path(base_path) / 'pdfs' / sanitize_filename(subfolder); save_dir.mkdir(parents=True, exist_ok=True)
        filepath = save_dir / filename
        try:
            with open(filepath, 'wb') as f: f.write(pdf_binary)
        except Exception as e: logger.error(f"Failed to save PDF {pmid} to {filepath}: {e}")

    async def _save_literature_to_disk(self, literature_data: Dict, collection_path: Optional[str], subfolder_prefix: str = ""):
         """Saves literature data (abstracts, fulltext, PDFs) to disk."""
         if not collection_path: self.emit_log("Collection path not set, cannot save literature.", "warn"); return
         total_saved_abstracts, total_saved_fulltext, total_saved_pdfs, total_failed = 0, 0, 0, 0
         for protein_or_key, lit_data in literature_data.items():
              if not lit_data or not isinstance(lit_data, dict): continue
              articles = lit_data.get('articles', [])
              articles = articles if isinstance(articles, list) else []
              subfolder_name = f"{subfolder_prefix}{sanitize_filename(protein_or_key)}"
              for article in articles:
                   if not self.is_running: return
                   if not isinstance(article, dict): total_failed += 1; logger.warning(f"Skipping invalid article data for {protein_or_key}"); continue
                   try:
                        self._save_article_abstract(article, collection_path, subfolder_name); total_saved_abstracts += 1
                        if article.get('full_text'): self._save_article_fulltext(article, collection_path, subfolder_name); total_saved_fulltext += 1
                        pdf_bytes = article.get('pdf_binary');
                        if pdf_bytes and isinstance(pdf_bytes, bytes): self._save_article_pdf(article, pdf_bytes, collection_path, subfolder_name); total_saved_pdfs += 1
                   except Exception as e: total_failed += 1; pmid = article.get('pmid', 'unknown'); logger.warning(f"Failed to save components for article {pmid} ({protein_or_key}): {e}")
         stage_name = "Stage 1" if "stage1" in subfolder_prefix else ("Stage 2" if "stage2" in subfolder_prefix else "Background")
         log_level = 'success' if total_failed == 0 else 'warn'
         self.emit_log(f"Saved {stage_name} Literature: Abstracts: {total_saved_abstracts} | Full-text: {total_saved_fulltext} | PDFs: {total_saved_pdfs} | Failed: {total_failed}", log_level)

    def _save_literature_metadata(self, collection_path: str, literature_data: Dict, inputs: Dict, stage: str):
        """Save metadata about the literature collection."""
        if not collection_path: return
        metadata_dir = Path(collection_path) / 'metadata'; metadata_dir.mkdir(parents=True, exist_ok=True)
        metadata_file = metadata_dir / f'{stage}_metadata.json'
        metadata = {'stage': stage, 'timestamp': datetime.now().isoformat(), 'inputs_snapshot': {'pollutant': inputs.get('pollutant'), 'proteins_analyzed_in_stage': list(literature_data.keys()), 'target_tissue': inputs.get('target_tissue'), 'literature_limit_per_protein': inputs.get('lit_limit')}, 'proteins': {}}
        total_articles_in_stage = 0
        for protein, data in literature_data.items():
             if data and isinstance(data, dict):
                  articles = data.get('articles', []); articles = articles if isinstance(articles, list) else []
                  metrics = data.get('quality_metrics', {}); metrics = metrics if isinstance(metrics, dict) else {}
                  metadata['proteins'][protein] = {'article_count': len(articles), 'avg_relevance': metrics.get('avg_relevance_score', 0), 'high_quality_count': metrics.get('high_quality_count', 0), 'fulltext_count': metrics.get('has_fulltext_count', 0)}
                  total_articles_in_stage += len(articles)
             else: logger.warning(f"Invalid literature data for '{protein}' during metadata save."); metadata['proteins'][protein] = {'article_count': 0, 'error': 'Invalid data'}
        metadata['total_articles_in_stage'] = total_articles_in_stage
        try:
            with open(metadata_file, 'w', encoding='utf-8') as f: json.dump(metadata, f, ensure_ascii=False, indent=2)
            self.emit_log(f'{stage.capitalize()} literature metadata saved.', 'info')
        except Exception as e: logger.error(f"Failed to save {stage} metadata: {e}"); self.emit_log(f'Failed to save {stage} metadata: {e}', 'warn')

    def _display_conclusion_index(self, response: Dict):
        """Displays a summary index of the final conclusion sections."""
        if not response or not isinstance(response, dict): return
        self.emit_log("Final Conclusion Index:", "info")
        display_keys = ['executive_summary', 'introduction', 'top_protein_analysis', 'integrated_mechanism', 'evidence_evaluation', 'validation_strategy', 'clinical_implications', 'conclusions', 'research_plans', 'quality_assessment']
        for key in display_keys:
             if key in response and response[key]: self.emit_log(f"- {key.replace('_',' ').title()}", "info")

    def _save_conclusion_to_file(self, response: Dict, inputs: Dict):
        """Saves the structured final conclusion to a text file."""
        if not response or not isinstance(response, dict): self.emit_log('No valid conclusion data to save.', 'warn'); return
        try:
            output_dir = Path('final_conclusions'); output_dir.mkdir(exist_ok=True)
            safe_pollutant = sanitize_filename(inputs.get('pollutant', 'unknown')); timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filepath = output_dir / f"{safe_pollutant}_conclusion_{timestamp}.txt"
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"FINAL CONCLUSION - MINI-REVIEW SUMMARY\nTopic: {inputs.get('pollutant')} toxicity in {inputs.get('target_tissue')}\nGenerated: {datetime.now().isoformat()}\n\n")
                section_order = ['executive_summary', 'introduction', 'top_protein_analysis', 'integrated_mechanism', 'evidence_evaluation', 'validation_strategy', 'clinical_implications', 'conclusions', 'research_plans', 'cited_pmids', 'word_count', 'quality_assessment', 'deep_analysis_metadata']
                for key in section_order:
                     if key in response and response[key]:
                          f.write(f"\n--- {key.replace('_',' ').upper()} ---\n"); value = response[key]
                          if isinstance(value, list):
                               if key == 'top_protein_analysis':
                                    for item in value: rank=item.get('rank','#'); prot=item.get('protein','N/A'); score=item.get('consensus_score','N/A'); score_str=f"{score:.1f}" if isinstance(score,(float,int)) else str(score); f.write(f"\n[{rank}. {prot} - Score: {score_str}]\n{item.get('analysis_text','')}\n")
                               elif key == 'research_plans':
                                    for item in value: prot=item.get('protein','N/A'); plan=item.get('plan','N/A'); f.write(f"\n[Plan for {prot}]\n{plan}\n")
                               else: f.write("\n".join(map(str, value)))
                          elif isinstance(value, dict) and key == 'deep_analysis_metadata':
                               for meta_key, meta_val in value.items(): f.write(f"- {meta_key.replace('_',' ').title()}: {meta_val}\n")
                          else: f.write(str(value)); f.write("\n")
            self.emit_log(f'Conclusion summary saved: {filepath}', 'success')
            if 'exports' not in self.session_data: self.session_data['exports'] = {}
            self.session_data['exports']['final_conclusion_txt'] = str(filepath)
        except Exception as e: logger.error(f"Failed to save conclusion file: {e}", exc_info=True); self.emit_log(f'Failed to save conclusion file: {e}', 'warn')

    def _save_full_review_text(self, text: str, inputs: Dict):
        """Saves the full text of the deep review."""
        if not text or not isinstance(text, str): self.emit_log('No valid full review text to save.', 'warn'); return
        try:
            output_dir = Path('final_conclusions'); output_dir.mkdir(exist_ok=True)
            safe_pollutant = sanitize_filename(inputs.get('pollutant', 'unknown')); timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filepath = output_dir / f"{safe_pollutant}_full_review_{timestamp}.txt"
            with open(filepath, 'w', encoding='utf-8') as f: f.write(f"DEEP RESEARCH MINI-REVIEW\nTopic: {inputs.get('pollutant')} toxicity in {inputs.get('target_tissue')}\n\n{text}")
            self.emit_log(f'Full review text saved: {filepath}', 'success')
            if 'exports' not in self.session_data: self.session_data['exports'] = {}
            self.session_data['exports']['full_review_txt'] = str(filepath)
        except Exception as e: logger.error(f"Failed to save full review text: {e}", exc_info=True); self.emit_log(f'Failed to save full review text: {e}', 'warn')

    def _format_protein_analysis_deep(self, text: str, proteins: List[Dict]) -> List[Dict]:
        """Formats deep protein analysis - using robust markdown splitting."""
        if not text or not isinstance(text, str): return []

        # Robust splitting by Markdown headers (## or ###)
        sections = re.split(r'(?:\n|^)(?:##|###)\s+(.*?)(?:\n|$)', text)
        
        result = []
        protein_map = {p_data['protein'].upper(): p_data for p_data in proteins}
        found_proteins = set()

        # Iterate sections: [header, content, header, content...]
        for i in range(1, len(sections), 2):
            header = sections[i].strip()
            content = sections[i+1].strip() if (i + 1) < len(sections) else ""
            
            if not content: continue

            matched_p_data = None
            for p_name_upper, p_data in protein_map.items():
                if p_name_upper in header.upper():
                    matched_p_data = p_data
                    break
            
            if matched_p_data:
                result.append({
                    'protein': matched_p_data['protein'],
                    'rank': proteins.index(matched_p_data) + 1,
                    'consensus_score': matched_p_data.get('final_consensus_score', 0),
                    'analysis_text': content
                })
                found_proteins.add(matched_p_data['protein'])

        # Add missing proteins
        for p_data in proteins:
            if p_data['protein'] not in found_proteins:
                result.append({
                    'protein': p_data['protein'],
                    'rank': proteins.index(p_data) + 1,
                    'consensus_score': p_data.get('final_consensus_score', 0),
                    'analysis_text': "[Analysis text extraction failed - Header not found]"
                })
        
        result.sort(key=lambda x: x['rank'])
        return result

    def _save_discussion_to_file(self, history: List[Dict], inputs: Dict, round_name: str):
        """Saves the discussion history to a text file."""
        if not history or not isinstance(history, list): self.emit_log(f'No valid {round_name} discussion history.', 'warn'); return
        try:
            output_dir = Path('discussions'); output_dir.mkdir(exist_ok=True)
            safe_pollutant = sanitize_filename(inputs.get('pollutant', 'unknown')); timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filepath = output_dir / f"{safe_pollutant}_{round_name}_discussion_{timestamp}.txt"
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"{round_name.upper()} EXPERT DISCUSSION\nTopic: {inputs.get('pollutant')} in {inputs.get('target_tissue')}\nGenerated: {datetime.now().isoformat()}\n\n")
                for entry in history:
                     if not isinstance(entry, dict): continue
                     f.write(f"--- {entry.get('speaker', 'Unknown')} ({entry.get('stage', '?')}) @ {entry.get('timestamp','')} ---\n{entry.get('content', 'No content.')}\n\n")
            self.emit_log(f'{round_name.capitalize()} discussion saved: {filepath}', 'success')
            if 'exports' not in self.session_data: self.session_data['exports'] = {}
            self.session_data['exports'][f'{round_name}_discussion_txt'] = str(filepath)
        except Exception as e: logger.error(f"Failed to save {round_name} discussion: {e}", exc_info=True); self.emit_log(f'Failed to save {round_name} discussion: {e}', 'warn')

    async def _generate_concise_detailed_report(self, inputs: Dict):
        """Generates a concise, user-friendly report for the Detailed Screening stage (Enhanced Version)."""
        if not self.ai_client:
            self.emit_log("AI client not available, skipping concise report.", 'error')
            self.session_data['final_concise_report'] = "# Report Generation Failed: AI Client not initialized."
            return

        try:
            # 1. Gather Basic Summaries
            r2_summary = self.session_data.get('round2_meeting', {}).get('final_summary', 'Summary not available.')
            
            # 2. Get Structured Data
            final_ranking_list = self.session_data.get('final_ranking', [])
            research_plans_list = self.session_data.get('research_plans', [])
            
            # 3. Build a "Rich Context" for the Top 5 Proteins
            # This pulls hard data from DB analysis and previous reasoning steps
            top_proteins = final_ranking_list[:5] # Focus detail on top 5
            rich_protein_context = []
            
            db_analysis = self.session_data.get('database_analysis', {})
            final_evals = self.session_data.get('round2_final_evaluations', [])
            critiques = self.session_data.get('round1_critiques', [])
            
            for protein in top_proteins:
                # A. Function (Database)
                p_db = db_analysis.get(protein, {}).get('functional_data', {}).get('uniprot_functions', {})
                funcs = p_db.get('functions', [])[:3] # Top 3 functions
                func_str = "; ".join(funcs) if funcs else "Specific function not annotated in UniProt."
                
                # B. Mechanism Hypothesis (From Round 2 Consensus)
                # Find the evaluation logic for this protein
                p_eval = next((e for e in final_evals if e.get('protein') == protein), {})
                mech_hypothesis = p_eval.get('revised_mechanistic_hypothesis', 'Mechanism requires further elucidation.')
                
                # C. Discussion Context (Pros/Cons inference)
                # We check if specific critiques mentioned this protein
                relevant_critiques = [c for c in critiques if protein.upper() in c.upper()]
                critique_str = "; ".join(relevant_critiques) if relevant_critiques else "No specific negative critiques in Round 1."

                rich_protein_context.append(
                    f"--- PROTEIN: {protein} ---\n"
                    f"1. Biological Function: {func_str}\n"
                    f"2. Proposed Mechanism of Toxicity: {mech_hypothesis}\n"
                    f"3. Expert Context/Critiques: {critique_str}\n"
                )
            
            rich_context_str = "\n".join(rich_protein_context)
            
            try:
                research_plans_json = json.dumps(research_plans_list[:3], indent=2) # Only top 3 plans
            except Exception:
                research_plans_json = "Could not serialize research plans."

            # 4. Construct the Enhanced Prompt
            system_prompt = "You are a Principal Investigator (PI). Your task is to generate a high-quality, scientifically rigorous Executive Summary of the target screening results. All output must be in English."

            user_prompt = f"""
[Research Task]
Pollutant: {inputs.get('pollutant')}
Target Tissue: {inputs.get('target_tissue')}
Phenotypes: {', '.join(inputs.get('target_phenotypes', []))}

[Input Data: Final Expert Consensus]
{r2_summary}

[Input Data: Detailed Protein Facts (Database & Mechanism)]
{rich_context_str}

[Input Data: Experimental Plans]
{research_plans_json}

[REPORT GENERATION INSTRUCTIONS]
Generate a "Final Strategic Report" strictly following the markdown structure below. 
Do not summarize generic process; focus on the specific biology of the targets.

### 1. Final Target Ranking
(List the Top 5 proteins simply).

### 2. Detailed Target Assessment (Top 3)
For each of the Top 3 proteins, provide a structured analysis block:

**1. [Protein Name]**
* **Biological Function:** (Summarize its normal physiological role based on the 'Biological Function' data provided).
* **Proposed Toxic Mechanism:** (Explain *how* the pollutant binding to this protein leads to the phenotype, using the 'Proposed Mechanism of Toxicity' data).
* **Selection Rationale (Strengths):** (Why did the experts rank this high? E.g., high expression, direct pathway link).
* **Potential Limitations/Risks:** (What are the weaknesses? E.g., lack of literature, speculative mechanism, or issues mentioned in 'Expert Context/Critiques').

### 3. Strategic Conclusion
(A short paragraph synthesizing why this set of targets represents the best path forward, acknowledging the balance between novelty and risk).

### 4. Next Step Experimental Plan
(Directly present the experimental plans for the Top 3 proteins provided in the input).
"""

            # 5. Generate
            report_markdown = await self.ai_client.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                is_json=False,
                temperature=0.3 # Slightly higher temp for better writing flow
            )

            self.session_data['final_concise_report'] = report_markdown

        except Exception as e:
            self.emit_log(f"Failed to generate concise detailed report: {e}", 'error')
            logger.error(f"Concise detailed report AI generation failed: {e}", exc_info=True)
            self.session_data['final_concise_report'] = f"# Report Generation Failed\n\nAn error occurred: {e}"

class AnalysisThread(QThread):
    """Worker thread for running asynchronous analysis tasks."""

    def __init__(self, engine: TwoStageAnalysisEngine, method_name: str, *args):
        super().__init__()
        self.engine = engine
        self.method_name = method_name
        self.args = args
        self._loop = None

    def run(self):
        try:
            try:
                self._loop = asyncio.get_event_loop()
                if self._loop.is_running():
                     self._loop = asyncio.new_event_loop(); asyncio.set_event_loop(self._loop)
            except RuntimeError: self._loop = asyncio.new_event_loop(); asyncio.set_event_loop(self._loop)

            method = getattr(self.engine, self.method_name)
            self._loop.run_until_complete(method(*self.args))
        except Exception as e:
            logger.error(f"Error in analysis thread ({self.method_name}): {e}", exc_info=True)
            if hasattr(self.engine, 'error_signal') and self.engine.error_signal:
                 self.engine.error_signal.emit(f"Thread Error ({self.method_name}): {e}")
        finally:
            if self._loop and not self._loop.is_closed():
                 pass
            logger.info(f"Analysis thread ({self.method_name}) finished.")
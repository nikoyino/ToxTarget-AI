# -*- coding: utf-8 -*-
"""
Quick Screening Engine Module
- Encapsulates all logic for the AI-driven quick screening workflow.
- Called by the main TwoStageAnalysisEngine.
"""

import logging
import asyncio
import re
import math
import statistics
import json
from typing import Dict, List, Optional, Tuple, Callable, TYPE_CHECKING

from PyQt5.QtCore import QObject

# Import shared constants from core_engine
from core_engine import (
    LANGUAGE_CONSTRAINT, 
    CORE_RESEARCH_PREMISE_POLLUTANT_AGNOSTIC
)

# For type hinting the parent engine to avoid circular import
if TYPE_CHECKING:
    from core_engine import TwoStageAnalysisEngine
    from services import AIService, BioDatabaseService

logger = logging.getLogger(__name__)

class QuickScreeningEngine(QObject):
    """
    Manages the Quick Screening workflow, delegated by the TwoStageAnalysisEngine.
    """

    # This constant is used ONLY by the quick screening methods
    ANGLE_RUBRIC_SCHEMES = {
        "Functional Relevance to Phenotype": {
            "functional_relevance": 50,
            "tissue_expression": 25,
            "pathway_centrality": 25
        },
        "Pathway Centrality and Network Position": {
            "functional_relevance": 30,
            "tissue_expression": 20,
            "pathway_centrality": 50
        },
        "Importance in Target Tissue": {
            "functional_relevance": 30,
            "tissue_expression": 50,
            "pathway_centrality": 20
        },
        "Balanced": {
            "functional_relevance": 40,
            "tissue_expression": 30,
            "pathway_centrality": 30
        }
    }

    def __init__(self, parent_engine: 'TwoStageAnalysisEngine', parent=None):
        super().__init__(parent)
        self.parent_engine = parent_engine
        self.ai_client: Optional['AIService'] = None
        self.bio_service: Optional['BioDatabaseService'] = None

    def set_services(self, ai_service: 'AIService', bio_service: 'BioDatabaseService'):
        """Inject initialized services from the parent engine."""
        self.ai_client = ai_service
        self.bio_service = bio_service

    # --- Proxy Methods to Parent Engine ---

    def emit_log(self, message: str, level: str = 'info'):
        self.parent_engine.emit_log(message, level)
    
    def emit_progress(self, percentage: int, main_text: str, detail_text: str = ''):
        self.parent_engine.emit_progress(percentage, main_text, detail_text)
    
    def is_running(self) -> bool:
        return self.parent_engine.is_running
    
    def set_session_data(self, key: str, value: any):
        """Store data in the parent engine's session_data."""
        self.parent_engine.session_data[key] = value
    
    def get_session_data(self, key: str, default: any = None) -> any:
        """Read data from the parent engine's session_data."""
        return self.parent_engine.session_data.get(key, default)

    # --- Main Workflow Method ---

    async def run_screening(self, inputs: Dict):
        """Performs a rapid screening focusing on functional criticality."""
        
        self.emit_log('Starting Quick Screening (Functional Criticality Focus)...', 'info')
        self.emit_log(f'Candidate proteins: {len(inputs["proteins"])}', 'info')
        self.emit_progress(0, 'Initializing Quick Screening', 'Setting up AI model')

        if not self.ai_client or not self.bio_service:
            self.emit_log('Services not initialized for QuickScreener. Aborting.', 'error')
            raise RuntimeError("QuickScreeningEngine services were not set by core_engine.")

        if not self.is_running(): return
        self.emit_progress(2, 'Normalizing Protein Names', 'Standardizing identifiers')
        
        normalized_map = await self.parent_engine._normalize_protein_names(
            inputs['proteins'], inputs.get('species_taxon', 9606)
        )
        original_to_normalized = normalized_map
        inputs['proteins'] = list(dict.fromkeys(normalized_map.values()))
        inputs['protein_name_mapping'] = original_to_normalized
        self.set_session_data('inputs', inputs)
        self.emit_log(f'Normalized {len(inputs["proteins"])} unique proteins.', 'success')

        if not self.is_running(): return
        self.emit_progress(5, 'Generating Research Hypothesis', 'AI contextualizing the study')
        
        await self.parent_engine._generate_hypothesis(inputs, use_pollutant_agnostic=True)
        if isinstance(self.get_session_data('hypothesis'), dict) and 'error' in self.get_session_data('hypothesis'):
            self.parent_engine.error_signal.emit(f'Hypothesis generation failed critically: {self.get_session_data("hypothesis")["error"]}')
            return

        if not self.is_running(): return
        self.emit_progress(10, 'Establishing Scoring Benchmarks', 'Defining reference points')
        
        if 'scoring_benchmarks' not in self.parent_engine.session_data:
            await self._establish_scoring_benchmark(inputs, use_pollutant_agnostic=True)

        if not self.is_running(): return
        self.emit_progress(15, 'Evaluating Proteins (Batch Mode)', 'AI assessing functional criticality')

        consistency_rounds = inputs.get('consistency_rounds', 1)
        if consistency_rounds > 1:
            quick_eval_results = await self._quick_screening_with_consensus(inputs, n_rounds=consistency_rounds)
        else:
            quick_eval_results = await self._quick_batch_evaluate_proteins(inputs, focus_angle="Functional Relevance to Phenotype")

        self.set_session_data('quick_eval_results', quick_eval_results)

        if not self.is_running(): return
        self.emit_progress(95, 'Ranking Proteins', 'Compiling final quick screening list')
        
        quick_screening_result = self._compile_quick_screening_results(quick_eval_results, inputs.get('top_n', 10))
        self.set_session_data('quick_screening_result', quick_screening_result)

        # --- Start of modification as per report_modification_plan.md ---
        if self.is_running():
            self.emit_progress(98, 'Generating Concise Report', 'AI summarizing top results')
            try:
                await self._generate_concise_quick_report(inputs, quick_screening_result)
                self.emit_log('Concise quick report generated.', 'success')
            except Exception as e:
                self.emit_log(f'Failed to generate concise quick report: {e}', 'error')
                logger.error(f"Concise quick report generation failed: {e}", exc_info=True)
                self.set_session_data('final_concise_report', f"# Report Generation Failed\n\nAn error occurred: {e}")
        # --- End of modification ---

        if not self.is_running(): return

        self.emit_progress(100, 'Quick Screening Complete', f'Top {inputs.get("top_n", 10)} proteins identified.')
        self.emit_log('Quick Screening workflow finished successfully.', 'success')
        
        self.parent_engine.quick_screening_complete_signal.emit(self.parent_engine.session_data)

    # --- All Private Helper Methods for Quick Screening ---

    async def _quick_screening_with_consensus(self, inputs: Dict, n_rounds: int) -> Dict:
        """Runs quick screening multiple times with different analytical angles and averages the scores."""
        self.emit_log(f'Starting consensus evaluation with {n_rounds} rounds...', 'info')
        all_rounds_evals = []

        analytical_angles_ordered = [
            "Functional Relevance to Phenotype",
            "Pathway Centrality and Network Position",
            "Importance in Target Tissue"
        ]
        
        if n_rounds == 1:
            focus_angles_for_rounds = [analytical_angles_ordered[0]]
        elif n_rounds == 2:
            focus_angles_for_rounds = [analytical_angles_ordered[0], analytical_angles_ordered[1]]
        elif n_rounds >= 3:
            focus_angles_for_rounds = [analytical_angles_ordered[i % len(analytical_angles_ordered)] for i in range(n_rounds)]
        else:
            focus_angles_for_rounds = [analytical_angles_ordered[0]]

        if 'scoring_benchmarks' not in self.parent_engine.session_data:
            self.emit_log('Scoring benchmarks not found! Establishing now...', 'warn')
            await self._establish_scoring_benchmark(inputs, use_pollutant_agnostic=True)

        for round_num, focus_angle in enumerate(focus_angles_for_rounds, 1):
            if not self.is_running(): break
            self.emit_log(f'Running evaluation round {round_num}/{n_rounds} (Focus: {focus_angle})...', 'info')
            
            round_evals = await self._quick_batch_evaluate_proteins(inputs, focus_angle=focus_angle)
            all_rounds_evals.append(round_evals)
            
            if round_num < n_rounds:
                await asyncio.sleep(1.0)

        if not all_rounds_evals:
            self.emit_log('Consensus evaluation aborted or failed.', 'error')
            return {}

        final_evals = {}
        protein_keys = set()
        for r_eval in all_rounds_evals:
             if isinstance(r_eval, dict): protein_keys.update(r_eval.keys())
        protein_keys = list(protein_keys)


        for protein in protein_keys:
            scores = []
            base_scores = []
            rationales = []
            binding_bonus = None

            for r_eval in all_rounds_evals:
                if isinstance(r_eval, dict) and protein in r_eval and r_eval.get(protein):
                    protein_eval = r_eval[protein]
                    scores.append(protein_eval.get('score', 0))
                    base_scores.append(protein_eval.get('base_score', 0))
                    rationales.append(protein_eval.get('rationale', 'N/A'))
                    
                    if binding_bonus is None:
                        binding_bonus = protein_eval.get('binding_bonus')

            valid_scores = [s for s in scores if s > 0] if any(s > 0 for s in scores) else scores
            mean_score = statistics.mean(valid_scores) if valid_scores else 0
            std_dev = statistics.stdev(valid_scores) if len(valid_scores) > 1 else 0

            mean_base_score = statistics.mean(base_scores) if base_scores else 0

            summary_rationale = ""
            if len(rationales) > 1 and valid_scores:
                max_score_val = max(valid_scores)
                max_score_index = scores.index(max_score_val)
                best_rationale = rationales[max_score_index]

                min_score_val = min(valid_scores)
                min_score_index = scores.index(min_score_val)
                worst_rationale = rationales[min_score_index]

                summary_rationale = (
                    f"Consensus evaluation suggests this protein is a high-priority candidate (Avg Score: {mean_score:.1f}). "
                    f"Scores varied across different analytical angles, from {min_score_val:.1f} to {max_score_val:.1f}. "
                    f"Its high ranking is primarily supported by its role in '{best_rationale}' (Score: {max_score_val:.1f}), "
                    f"while its lowest score was due to '{worst_rationale}' (Score: {min_score_val:.1f})."
                )
            elif rationales:
                summary_rationale = rationales[0]
            else:
                summary_rationale = 'Rationale not available.'

            final_evals[protein] = {
                'score': round(mean_score, 2),
                'std_dev': round(std_dev, 2),
                'rationale': summary_rationale,
                'score_range': f"{min(scores)}-{max(scores)}" if scores else "N/A",
                'scores_per_round': [round(s, 1) for s in scores],
                'base_score': round(mean_base_score, 2), 
                'binding_bonus': binding_bonus if binding_bonus is not None else 0
            }

            self.emit_log(
                f"  Consensus for {protein}: Avg Score={mean_score:.2f}, StdDev={std_dev:.2f}, Scores={final_evals[protein]['scores_per_round']}",
                'info'
            )

            if std_dev > 15:
                self.emit_log(
                    f"⚠️ {protein}: High score variance (std={std_dev:.2f}, scores={[round(s, 1) for s in scores]}) across rounds.",
                    'warning'
                )

        self.emit_log('Consensus evaluation complete.', 'success')
        return final_evals

    async def _establish_scoring_benchmark(self, inputs: Dict, use_pollutant_agnostic: bool = False):
        """Establishes scoring benchmarks using AI for consistency."""
        if self.get_session_data('scoring_benchmarks'):
            self.emit_log('Using existing scoring benchmarks for this session.', 'info')
            return

        self.emit_log('Establishing scoring benchmarks for this session...', 'info')
        phenotypes_str = ', '.join(inputs.get('target_phenotypes', []))
        tissue = inputs['target_tissue']

        if use_pollutant_agnostic:
            benchmark_context = f"evaluating intrinsic functional criticality related to '{phenotypes_str}' in {tissue} upon functional disruption"
        else:
            benchmark_context = f"evaluating toxicological relevance related to {inputs['pollutant']}-induced {phenotypes_str} in {tissue}"

        # --- FIX START ---
        
        system_prompt_content = f"""As a toxicology research expert, you will establish scoring benchmarks.
{LANGUAGE_CONSTRAINT}
[TASK] Create 5 benchmark proteins for {benchmark_context}.
[REQUIRED - 5 LEVELS] Total 0-100 points:
- 90-100 (Exemplary): Central role example
- 70-89 (Strong): Clear involvement example
- 50-69 (Moderate): Plausible link example
- 30-49 (Weak): Marginal connection example
- 0-29 (Minimal): Unlikely involvement example
[JSON]
{{"benchmarks":[{{"score_level":"90-100","example_protein":"Name","reasoning":"Brief why"}}]}}
Include all 5 levels. Brief reasoning for each."""
        
        user_prompt_content = "Please generate the 5-level scoring benchmarks as specified in the system instructions."

        try:
            if not self.ai_client:
                 raise RuntimeError("AI Service not initialized for benchmark generation.")
            
            benchmarks = await self.ai_client.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=True, 
                temperature=0.0
            )
            
            # --- FIX END ---
            
            if benchmarks and isinstance(benchmarks.get('benchmarks'), list) and len(benchmarks['benchmarks']) >= 3:
                self.set_session_data('scoring_benchmarks', benchmarks)
                self.emit_log('Scoring benchmarks established successfully.', 'success')
                benchmark_summary = "\n".join([f"  - Score {b.get('score_level', '?')}: {b.get('example_protein', '?')} ({b.get('reasoning', '')[:50]}...)" for b in benchmarks['benchmarks']])
                self.emit_log(f"Benchmarks:\n{benchmark_summary}", 'debug')
            else:
                self.emit_log('Failed to establish valid scoring benchmarks (invalid format or insufficient entries).', 'warn')
                self.set_session_data('scoring_benchmarks', {})
        except Exception as e:
            self.emit_log(f'Error establishing scoring benchmarks: {e}', 'error')
            logger.error(f"Benchmark generation failed: {e}", exc_info=True)
            self.set_session_data('scoring_benchmarks', {})

    def _fuzzy_match_protein(self, returned_name: str, protein_list: List[str]) -> Optional[str]:
        """
        Fuzzy match protein name, handling case, special characters, etc.
        """
        if not returned_name:
            return None

        returned_upper = returned_name.upper().strip()

        for p in protein_list:
            if p.upper().strip() == returned_upper:
                return p

        returned_clean = re.sub(r'[^A-Z0-9]', '', returned_upper)
        for p in protein_list:
            p_clean = re.sub(r'[^A-Z0-9]', '', p.upper())
            if p_clean == returned_clean:
                return p

        from difflib import SequenceMatcher
        best_match = None
        best_ratio = 0.85
        for p in protein_list:
            ratio = SequenceMatcher(None, returned_upper, p.upper()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = p

        if best_match:
            logger.info(f"Fuzzy matched '{returned_name}' to '{best_match}' (similarity: {best_ratio:.2f})")

        return best_match

    def _build_angle_specific_rubric(self, focus_angle: str, phenotypes_str: str, tissue: str) -> str:
        """Build scoring rubric with weights adjusted for current analytical angle."""
        weights = self.ANGLE_RUBRIC_SCHEMES.get(focus_angle, self.ANGLE_RUBRIC_SCHEMES["Balanced"])
        
        rubric = f"""
[RUBRIC - 100pts] ANALYTICAL FOCUS: {focus_angle}
Weights adjusted to emphasize the current analytical angle.

1. Functional Relevance (0-{weights['functional_relevance']}): Direct pathway link to {phenotypes_str}
   {weights['functional_relevance']}=Core pathway protein | {int(weights['functional_relevance']*0.75)}=Key regulator | {int(weights['functional_relevance']*0.5)}=Moderate involvement | {int(weights['functional_relevance']*0.25)}=Weak connection | 0=No relevance

2. Tissue Importance (0-{weights['tissue_expression']}): Expression and functional role in {tissue}
   {weights['tissue_expression']}=Highly expressed+critical | {int(weights['tissue_expression']*0.67)}=Moderately important | {int(weights['tissue_expression']*0.33)}=Low expression/redundant | 0=Not relevant in tissue

3. Pathway Centrality (0-{weights['pathway_centrality']}): Network position and pathway control
   {weights['pathway_centrality']}=Essential bottleneck | {int(weights['pathway_centrality']*0.67)}=Important node | {int(weights['pathway_centrality']*0.33)}=Peripheral role | 0=Not in relevant networks
"""
        return rubric

    def _build_enhanced_focus_instruction(self, focus_angle: str) -> str:
        """Build detailed, actionable focus instruction with a consistent anchor."""
        
        core_focus_instruction = f"""
[CORE FOCUS - ALWAYS CONSIDER]
The ultimate goal is to identify proteins whose functional disruption is most critical for causing the target phenotypes. 
Regardless of the angle, your final score MUST reflect the protein's potential to cause these specific phenotypes. 
A protein with high tissue expression or pathway centrality but NO plausible link to the phenotype should receive a LOW score.
"""

        angle_perspectives = {
            "Functional Relevance to Phenotype": """
[CURRENT ANGLE: Functional Mechanisms and Direct Causality]
From this perspective, prioritize proteins with the most DIRECT and MECHANISTIC links to the target phenotypes. 
Ask: How clear and well-documented is the chain of events from this protein's malfunction to the specific cellular damage observed in the phenotype?
A high score requires strong evidence of direct involvement.
""",
            "Pathway Centrality and Network Position": """
[CURRENT ANGLE: Network Impact on the Phenotype]
From this perspective, evaluate how a protein's position in a network AMPLIFIES its impact on the target phenotypes. 
Ask: If this protein's function is disrupted, would it cause a cascading failure within pathways known to be critical for the phenotype?
A high score requires the protein to be a bottleneck or hub in a *phenotype-relevant* pathway.
""",
            "Importance in Target Tissue": """
[CURRENT ANGLE: Tissue-Specific Vulnerability]
From this perspective, consider how a protein's role in the target tissue makes the tissue specifically vulnerable to the observed phenotypes.
Ask: Is this protein so highly expressed or functionally critical in THIS tissue that its disruption would disproportionately lead to the phenotype, compared to other tissues?
A high score requires that the protein's importance is *context-specific* to the tissue and *directly relevant* to the phenotype.
"""
        }
        
        angle_instruction = angle_perspectives.get(focus_angle, angle_perspectives["Functional Relevance to Phenotype"])
        
        weights = self.ANGLE_RUBRIC_SCHEMES.get(focus_angle, self.ANGLE_RUBRIC_SCHEMES["Balanced"])
        angle_instruction += f"""
NOTE: Rubric weights are adjusted for this angle (e.g., {focus_angle.split(' ')[0]} = {weights[list(weights.keys())[0]]} pts). 
Use this angle as a lens to evaluate the protein's primary link to the phenotype.
"""
        
        return core_focus_instruction + angle_instruction

    async def _quick_batch_evaluate_proteins(self, inputs: Dict, focus_angle: Optional[str] = None) -> Dict:
        """Evaluates proteins in batches using AI with standardized rubric, benchmarks, and an optional analytical focus."""
        proteins = inputs['proteins']
        batch_size = 5
        max_retries = 3
        all_evals = {}
        total_batches = math.ceil(len(proteins) / batch_size)
        phenotypes_str = ', '.join(inputs.get('target_phenotypes', []))
        tissue = inputs['target_tissue']

        premise = CORE_RESEARCH_PREMISE_POLLUTANT_AGNOSTIC.format(
            target_tissue=tissue,
            target_phenotypes=phenotypes_str
        )
        evaluation_task_description = f"Evaluate the FUNCTIONAL CRITICALITY of the following proteins regarding their potential to cause {phenotypes_str} in {tissue} if their function is disrupted, based ONLY on your internal knowledge."

        if focus_angle:
            scoring_rubric = self._build_angle_specific_rubric(focus_angle, phenotypes_str, tissue)
        else:
            scoring_rubric = self._build_angle_specific_rubric("Balanced", phenotypes_str, tissue)

        benchmarks = self.get_session_data('scoring_benchmarks', {})
        benchmark_text = "No benchmarks available"
        if benchmarks and isinstance(benchmarks.get('benchmarks'), list):
            benchmark_text = " | ".join([
                f"{b.get('score_level', 'N/A')}:{b.get('example_protein', 'N/A')[:15]}"
                for b in benchmarks.get('benchmarks', [])
            ])

        focus_instruction = ""
        if focus_angle:
            focus_instruction = self._build_enhanced_focus_instruction(focus_angle)

        for i in range(0, len(proteins), batch_size):
            if not self.is_running(): return all_evals
            batch_num = (i // batch_size) + 1
            batch_proteins = proteins[i:i+batch_size]
            import time
            batch_start_time = time.time()
            self.emit_progress(
                15 + int((batch_num / total_batches) * 80),
                f'Evaluating Batch {batch_num}/{total_batches}',
                f'Proteins: {batch_proteins[0]}...'
            )

            protein_bonuses = {}
            total_proteins = len(proteins)
            max_bonus = (total_proteins / 30.0) * 10.0
            for idx, prot in enumerate(batch_proteins):
                try:
                    protein_idx = proteins.index(prot)
                    if total_proteins > 1:
                        bonus = max_bonus * (1 - protein_idx / (total_proteins - 1))
                    else:
                        bonus = max_bonus
                    protein_bonuses[prot] = round(bonus)
                except ValueError:
                    protein_bonuses[prot] = 0
                    self.emit_log(f"Warning: Protein '{prot}' not found in original list for bonus calculation.", 'warn')


            protein_list_with_bonus = "\n".join([
                f"- {p} (Binding bonus: +{protein_bonuses.get(p, 0)} pts)"
                for p in batch_proteins
            ])

            current_weights = self.ANGLE_RUBRIC_SCHEMES.get(
                focus_angle if focus_angle else "Balanced",
                self.ANGLE_RUBRIC_SCHEMES["Balanced"]
            )

            # --- FIX START ---
            
            system_prompt_content = f"""{LANGUAGE_CONSTRAINT}
[CRITICAL] Evaluate ALL {len(batch_proteins)} proteins. Never skip. If uncertain, score 10-20 but include. Output count must match input.
{premise}
[TASK] {evaluation_task_description}
{focus_instruction}
[BENCHMARKS] {benchmark_text}
{scoring_rubric}
[JSON OUTPUT]
{{"evaluations":[{{"protein":"NAME","scores":{{"functional_relevance":0-{current_weights['functional_relevance']},"tissue_expression":0-{current_weights['tissue_expression']},"pathway_centrality":0-{current_weights['pathway_centrality']}}},"base_score":0-100,"rationale":"brief"}}]}}
Rules: Integers only | Base_score=sum(scores) | Use benchmarks | Brief rationale | Include all {len(batch_proteins)} proteins
Note: Binding bonuses will be added automatically to base_score."""

            user_prompt_content = f"""
[PROTEINS] Proteins are pre-sorted by binding affinity (strongest first). Bonuses reflect binding strength.
{protein_list_with_bonus}
"""
            
            # --- FIX END ---
            
            batch_success = False
            for attempt in range(max_retries):
                if not self.is_running(): return all_evals
                try:
                    if not self.ai_client:
                         raise RuntimeError("AI Service not initialized for batch evaluation.")

                    response = await self.ai_client.generate(
                        system_prompt=system_prompt_content,
                        user_prompt=user_prompt_content,
                        is_json=True, 
                        temperature=0.35
                    )

                    if isinstance(response, list) and len(response) == 1 and isinstance(response[0], dict):
                        logger.warning(f"Batch {batch_num} (Attempt {attempt+1}): AI returned list containing dict, extracting dict.")
                        response_dict = response[0]
                    elif isinstance(response, dict):
                        response_dict = response
                    else:
                        raise ValueError(f"AI response is neither a dict nor a list containing one dict. Type: {type(response)}")

                    evaluations = response_dict.get('evaluations', [])
                    count_in_batch = 0
                    processed_proteins_in_batch = set()
                    batch_evals_temp = {}

                    if isinstance(evaluations, list):
                        for item in evaluations:
                            if not isinstance(item, dict):
                                 raise ValueError(f"Invalid item format in AI response list: {item}")

                            protein = item.get('protein')
                            matched_protein_name = next((p for p in batch_proteins if protein and p.upper() == protein.upper()), None)

                            if matched_protein_name:
                                if matched_protein_name in processed_proteins_in_batch:
                                     self.emit_log(f'Batch {batch_num} (Attempt {attempt+1}): Duplicate evaluation for "{matched_protein_name}". Using first.', 'warn')
                                     continue

                                processed_proteins_in_batch.add(matched_protein_name)
                                count_in_batch += 1
                                base_score = item.get('base_score', item.get('total_score', 0))
                                bonus = protein_bonuses.get(matched_protein_name, 0)
                                final_score = base_score + bonus
                                rationale = item.get('rationale', 'Rationale not provided.') if isinstance(item.get('rationale'), str) else 'Invalid rationale format.'
                                if final_score != bonus and ("cannot evaluate" in rationale.lower() or "insufficient information" in rationale.lower()):
                                    final_score = bonus
                                    self.emit_log(f'Batch {batch_num} (Attempt {attempt+1}): Corrected score to {bonus} (bonus only) for "{matched_protein_name}" based on rationale.', 'debug')

                                batch_evals_temp[matched_protein_name] = {
                                    'score': final_score if isinstance(final_score, (int, float)) else 0,
                                    'rationale': rationale,
                                    'score_details': item.get('scores', {}) if isinstance(item.get('scores'), dict) else {},
                                    'base_score': base_score,
                                    'binding_bonus': bonus,
                                    'scores_per_round': [final_score]
                                }

                            elif protein:
                                 self.emit_log(f'Batch {batch_num} (Attempt {attempt+1}): AI returned unexpected protein "{protein}". Skipping.', 'warn')

                        if len(processed_proteins_in_batch) == len(batch_proteins):
                            all_evals.update(batch_evals_temp)
                            batch_success = True
                            self.emit_log(f'Batch {batch_num}/{total_batches} evaluated successfully on attempt {attempt+1}.', 'success')

                            batch_duration = time.time() - batch_start_time
                            self.emit_log(
                                f'⏱️  Batch {batch_num} duration: {batch_duration:.1f}s '
                                f'({batch_duration/len(batch_proteins):.1f}s per protein)',
                                'info'
                            )

                            if batch_duration > 180:
                                self.emit_log(
                                    f'⚠️  Batch {batch_num} took too long! Check model and network',
                                    'warning'
                                )

                            break
                        else:
                            missing = [p for p in batch_proteins if p not in processed_proteins_in_batch]
                            raise ValueError(f"AI response missing evaluations for {len(missing)} proteins: {', '.join(missing[:3])}...")

                    else:
                        raise ValueError(f"AI response 'evaluations' field was not a list. Type: {type(evaluations)}")

                except Exception as e:
                    logger.warning(f"Quick evaluation failed for batch {batch_num}, attempt {attempt+1}/{max_retries}: {e}")
                    self.emit_log(f'Batch {batch_num} evaluation attempt {attempt+1} failed: {e}', 'warn')
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.0 * (attempt + 1))
                    else:
                        self.emit_log(f'Batch {batch_num} failed after {max_retries} attempts. Assigning default scores.', 'error')
                        for protein in batch_proteins:
                            if protein not in all_evals:
                                all_evals[protein] = {
                                    'score': protein_bonuses.get(protein, 0),
                                    'rationale': f'AI evaluation failed after {max_retries} attempts.',
                                    'score_details': {},
                                    'base_score': 0,
                                    'binding_bonus': protein_bonuses.get(protein, 0),
                                    'scores_per_round': [protein_bonuses.get(protein, 0)]
                                }


            await asyncio.sleep(0.5)

        return all_evals

    async def _resolve_tied_proteins(
        self,
        tied_proteins: List[str],
        tied_score: float,
        inputs: Dict
    ) -> Dict[str, float]:
        """Re-evaluate tied proteins at cutoff boundary."""
        self.emit_log(f'Detected {len(tied_proteins)} proteins tied at {tied_score}, starting refined evaluation...', 'info')

        phenotypes_str = ', '.join(inputs.get('target_phenotypes', []))
        premise = CORE_RESEARCH_PREMISE_POLLUTANT_AGNOSTIC.format(
            target_tissue=inputs['target_tissue'],
            target_phenotypes=phenotypes_str
        )

        protein_list_str = "\n".join([f"- {p}" for p in tied_proteins])

        # --- FIX START ---
        
        system_prompt_content = f"""{LANGUAGE_CONSTRAINT}
[CRITICAL] Evaluate ALL {len(tied_proteins)} proteins. Never skip.
{premise}
[TASK] Tie-breaker for {len(tied_proteins)} proteins scored {tied_score}/100. Use refined criteria.
[REFINED RUBRIC - 100pts Total]
1. Functional Directness (0-20): {phenotypes_str} link | 20=Direct 10=Indirect 0=Distant
2. Tissue Specificity (0-20): {inputs['target_tissue']} importance | 20=High 10=Moderate 0=Low
3. Pathway Vulnerability (0-20): Critical node | 20=Bottleneck 10=Important 0=Redundant
4. Mechanism Clarity (0-20): Evidence | 20=Clear 10=Plausible 0=Speculative
5. Druggability (0-20): Target feasibility | 20=Easy 10=Feasible 0=Difficult
[JSON]
{{"evaluations":[{{"protein":"NAME","scores":{{"functional_directness":0-20,"tissue_importance":0-20,"pathway_vulnerability":0-20,"mechanism_clarity":0-20,"intervention_potential":0-20}},"total_score":0-100,"rationale":"brief differentiator"}}]}}
Rules: 0-20 each | Total=sum | Highlight differences | All {len(tied_proteins)} proteins"""
        
        user_prompt_content = f"""
[TIED PROTEINS]
{protein_list_str}
"""
        
        # --- FIX END ---
        
        try:
            if not self.ai_client:
                 raise RuntimeError("AI Service not initialized for tie-breaking.")
            
            response = await self.ai_client.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=True, 
                temperature=0.2
            )

            if isinstance(response, list) and len(response) == 1 and isinstance(response[0], dict):
                logger.warning("Tie-breaking AI returned list containing dict, extracting dict.")
                response_dict = response[0]
            elif isinstance(response, dict):
                response_dict = response
            else:
                raise ValueError(f"Tie-breaking AI response is neither a dict nor a list containing one dict. Type: {type(response)}")

            evaluations = response_dict.get('evaluations', [])


            refined_scores = {}
            found_proteins = set()
            for item in evaluations:
                protein = item.get('protein')
                matched_protein = next((tied_p for tied_p in tied_proteins if protein and tied_p.upper() == protein.upper()), None)

                if matched_protein:
                    found_proteins.add(matched_protein)
                    total = item.get('total_score', tied_score)
                    try: refined_score_val = float(total)
                    except (ValueError, TypeError): refined_score_val = tied_score; self.emit_log(f'  {matched_protein}: Invalid tie-break score ({total}), using original {tied_score}', 'warn')
                    refined_scores[matched_protein] = round(refined_score_val, 2)
                    scores_detail = item.get('scores', {})
                    self.emit_log(
                        f"  {matched_protein}: Refined Score {refined_score_val:.2f}/100 "
                        f"[Func={scores_detail.get('functional_directness',0)}, "
                        f"Tissue={scores_detail.get('tissue_importance',0)}, "
                        f"Vuln={scores_detail.get('pathway_vulnerability',0)}, "
                        f"Clarity={scores_detail.get('mechanism_clarity',0)}, "
                        f"Interv={scores_detail.get('intervention_potential',0)}]",
                        'success'
                    )
                elif protein:
                     self.emit_log(f'AI returned tie-break score for unexpected protein "{protein}". Skipping.', 'warn')

            missing_in_response = [p for p in tied_proteins if p not in found_proteins]
            for protein in missing_in_response:
                refined_scores[protein] = tied_score
                self.emit_log(f"  {protein}: Not re-evaluated by AI, kept original score {tied_score}", 'warn')
            return refined_scores

        except Exception as e:
            self.emit_log(f'Refined evaluation failed: {e}, using original scores for all tied proteins.', 'error')
            logger.error(f"Tie-breaking evaluation failed: {e}", exc_info=True)
            return {p: tied_score for p in tied_proteins}

    def _compile_quick_screening_results(self, quick_evals: Dict, top_n: int) -> Dict:
        """Compile quick screening results with tie-breaking logic."""
        if not quick_evals:
             self.emit_log('No evaluation data available to compile results.', 'warn')
             return {'top10': [], 'full_ranking': [], 'total_evaluated': 0, 'score_distribution': {'high': 0, 'medium': 0, 'low': 0}}

        ranked_list_tuples = sorted(
            quick_evals.items(),
            key=lambda item: item[1].get('score', 0),
            reverse=True
        )
        ranked_list = [{'protein': p, **data} for p, data in ranked_list_tuples]

        if len(ranked_list) > top_n:
            try:
                cutoff_score_val = ranked_list[top_n - 1].get('score')
                score_after_cutoff_val = ranked_list[top_n].get('score') if top_n < len(ranked_list) else None

                if isinstance(cutoff_score_val, (int, float)) and isinstance(score_after_cutoff_val, (int, float)):
                    cutoff_score = float(cutoff_score_val)
                    score_after_cutoff = float(score_after_cutoff_val)

                    if math.isclose(cutoff_score, score_after_cutoff):
                        tied_at_cutoff = [item for item in ranked_list if math.isclose(item.get('score', -1), cutoff_score)]
                        tied_proteins = [item['protein'] for item in tied_at_cutoff]

                        self.emit_log(
                            f'Detected {len(tied_proteins)} proteins tied at boundary score {cutoff_score:.2f}. '
                            'Tie-breaking refinement is currently disabled due to an event loop conflict. Original ranks will be used for tied proteins.',
                            'warn'
                        )
                else:
                     logger.debug("Scores at cutoff boundary are not equal or not valid numbers, skipping tie-breaking.")


            except (ValueError, TypeError, KeyError, IndexError) as e:
                self.emit_log(f"Could not check for tie-breaking due to data issue: {e}", 'warn')
                logger.warning(f"Tie-breaking check skipped: {e}")
            except Exception as tie_break_e:
                 self.emit_log(f"Unexpected error during tie-breaking check: {tie_break_e}. Proceeding with original ranks.", 'error')
                 logger.error(f"Tie-breaking check failed: {tie_break_e}", exc_info=True)

        top_list_final = []
        full_ranking_final = []
        score_distribution = {'high': 0, 'medium': 0, 'low': 0}

        for i, item_data in enumerate(ranked_list):
            protein = item_data['protein']
            score = item_data.get('score', 0)
            rationale = item_data.get('rationale', '')
            is_top_n = (i < top_n)

            base_score = item_data.get('base_score')
            binding_bonus = item_data.get('binding_bonus')
            scores_per_round = item_data.get('scores_per_round')

            breakdown_parts = []
            if base_score is not None:
                if scores_per_round and len(scores_per_round) > 1:
                     breakdown_parts.append(f"Scores per Round={scores_per_round}")
                     if binding_bonus is not None and binding_bonus > 0:
                         breakdown_parts.append(f"Affinity Bonus=+{binding_bonus}")
                     elif binding_bonus is not None and binding_bonus == 0:
                         breakdown_parts.append(f"Affinity Bonus=0")
                elif binding_bonus is not None and binding_bonus > 0:
                    breakdown_parts.append(f"Base={base_score} + Affinity Bonus={binding_bonus}")
                elif binding_bonus is not None and binding_bonus == 0:
                     breakdown_parts.append(f"Base={base_score} (No Affinity Bonus)")


            if breakdown_parts:
                rationale += f" [Score Breakdown: {'; '.join(breakdown_parts)}]"

            if item_data.get('tie_broken'):
                orig_score = item_data.get('original_score', 'N/A')
                orig_score_str = f"{orig_score:.2f}" if isinstance(orig_score, (float, int)) else str(orig_score)
                rationale += f" [Refined Score: {score:.2f}/100 from original {orig_score_str}]"

            try: score_float = float(score)
            except (ValueError, TypeError): score_float = 0.0

            full_item = {
                'rank': i + 1, 'protein': protein, 'final_score': score_float,
                'round1_score': score_float, 'rationale': rationale, 'in_top10': is_top_n,
                'base_score': item_data.get('base_score'),
                'binding_bonus': item_data.get('binding_bonus'),
                'scores_per_round': item_data.get('scores_per_round')
            }
            full_ranking_final.append(full_item)

            if is_top_n:
                top_list_final.append({
                    'rank': i + 1, 'protein': protein, 'final_score': score_float,
                    'round1_score': score_float, 'rationale': rationale,
                    'base_score': item_data.get('base_score'),
                    'binding_bonus': item_data.get('binding_bonus'),
                    'scores_per_round': item_data.get('scores_per_round')
                })

            if score_float >= 70: score_distribution['high'] += 1
            elif score_float >= 50: score_distribution['medium'] += 1
            else: score_distribution['low'] += 1

        result = {
            'top10': top_list_final, 'full_ranking': full_ranking_final,
            'total_evaluated': len(ranked_list), 'score_distribution': score_distribution
        }

        top_summary = "\n".join([f"- #{item['rank']} {item['protein']} ({item['final_score']:.2f})" for item in top_list_final])
        self.emit_log(f'Quick Screening Top {top_n} Results:\n{top_summary if top_summary else "No proteins in top list."}', 'info') 

        if top_list_final:
            self.emit_log(f'Quick ranking compiled. Top protein: {top_list_final[0]["protein"]} ({top_list_final[0]["final_score"]:.2f})', 'success')
        else:
             self.emit_log('Quick ranking compiled, but no proteins made it to the top list.', 'warn')

        return result

    # --- Start of new method as per report_modification_plan.md ---
    async def _generate_concise_quick_report(self, inputs: Dict, screening_result: Dict):
        """
        Generates a concise, user-friendly report for the Quick Screening stage
        based on report_modification_plan.md.
        """
        if not self.ai_client:
            self.emit_log("AI client not available, skipping concise report.", 'error')
            self.set_session_data('final_concise_report', "# Report Generation Failed: AI Client not initialized.")
            return

        top_10_list = screening_result.get('top10', [])
        if not top_10_list:
            self.emit_log("No top 10 proteins to report.", 'info')
            self.set_session_data('final_concise_report', "# Quick Screening Report\n\nNo proteins were ranked in the top 10.")
            return

        try:
            top_10_json = json.dumps(top_10_list, indent=2)
        except Exception as e:
            logger.error(f"Failed to serialize top 10 list: {e}")
            top_10_json = f"Error: Could not display protein list ({e})"

        phenotypes_str = ", ".join(inputs.get('target_phenotypes', [])) or "the specified toxic phenotypes"
        top_n = len(top_10_list)

        # Prompt template from report_modification_plan.md
        system_prompt = "You are a senior toxicology analyst, responsible for writing a concise and clear quick screening report. All output must be in English."
        
        user_prompt = f"""
Based on the following research context and quick screening results, generate a summary report.

[Research Context]
- Pollutant: {inputs.get('pollutant', 'N/A')}
- Target Tissue: {inputs.get('target_tissue', 'N/A')}
- Toxic Phenotypes: {phenotypes_str}

[Quick Screening Results (Top {top_n})]
{top_10_json}

[Report Task]
Please generate a report strictly following this format. For each protein in the list:
1.  State its name and score.
2.  Based on your internal knowledge, provide a one-sentence summary of its core function.
3.  Based on your internal knowledge, propose a one-sentence hypothesis for how it "might cause '{phenotypes_str}'".

Format:
"Through quick screening, the top {top_n} proteins identified are:

1.  **[Protein 1 Name] (Score: [Score])**
    -   **Function:** [AI generates one-sentence function here]
    -   **Mechanism Hypothesis:** This protein might cause '{phenotypes_str}' by [AI generates one-sentence hypothesis here].

2.  **[Protein 2 Name] (Score: [Score])**
    -   **Function:** [AI generates one-sentence function here]
    -   **Mechanism Hypothesis:** This protein might cause '{phenotypes_str}' by [AI generates one-sentence hypothesis here].

(Continue for all {top_n} proteins in the provided JSON list)"
"""
        
        try:
            report_markdown = await self.ai_client.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                is_json=False,
                temperature=0.2 # Low temp for factual summary
            )
            self.set_session_data('final_concise_report', report_markdown)
        
        except Exception as e:
            self.emit_log(f"Failed to generate concise quick report: {e}", 'error')
            logger.error(f"Concise quick report AI generation failed: {e}", exc_info=True)
            self.set_session_data('final_concise_report', f"# Report Generation Failed\n\nAn error occurred: {e}")
    # --- End of new method ---
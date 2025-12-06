# -*- coding: utf-8 -*-
"""
Function-Phenotype Matching Analyzer - Extended Version
Objective analysis, batch processing, and comparative summary generation.
"""

import logging
import asyncio # Added for potential async operations if needed later
from typing import Dict, List, Optional, Any # Added Optional, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class FunctionPhenotypeAnalyzer:
    """Function-Phenotype Matching Analyzer - Objective analysis, batch processing"""

    def __init__(self):
        # Existing phenotype mapping
        self.phenotype_function_map = {
            'hepatotoxicity': {
                'related_functions': [
                    'xenobiotic metabolism', 'drug metabolism', 'phase I metabolism',
                    'phase II metabolism', 'lipid metabolism', 'glucose metabolism',
                    'oxidative stress response', 'apoptosis', 'mitochondrial function'
                ],
                'related_pathways': [
                    'cytochrome P450', 'glutathione metabolism', 'fatty acid metabolism',
                    'oxidative phosphorylation', 'apoptosis', 'NAFLD'
                ],
                'key_biomarkers': ['ALT', 'AST', 'APAP', 'bile acid']
            },
            'nephrotoxicity': {
                'related_functions': [
                    'transport', 'ion homeostasis', 'filtration', 'reabsorption',
                    'oxidative stress response', 'inflammation'
                ],
                'related_pathways': [
                    'proximal tubule transport', 'collecting duct function',
                    'renin-angiotensin system', 'acute kidney injury'
                ],
                'key_biomarkers': ['creatinine', 'BUN', 'KIM-1']
            },
            'neurotoxicity': {
                'related_functions': [
                    'neurotransmission', 'synaptic function', 'ion channel',
                    'neuronal development', 'myelination', 'oxidative stress'
                ],
                'related_pathways': [
                    'dopaminergic synapse', 'glutamatergic synapse', 'cholinergic synapse',
                    'neurodegeneration', 'calcium signaling'
                ],
                'key_biomarkers': ['dopamine', 'GABA', 'acetylcholine']
            },
            'cardiotoxicity': {
                'related_functions': [
                    'contractility', 'ion channel', 'calcium handling',
                    'mitochondrial function', 'oxidative stress'
                ],
                'related_pathways': [
                    'cardiac muscle contraction', 'calcium signaling',
                    'adrenergic signaling', 'hypertrophic cardiomyopathy'
                ],
                'key_biomarkers': ['troponin', 'BNP', 'CK-MB']
            }
            # Add more phenotypes as needed
        }

    # === Existing method - analyze_function_phenotype_match ===
    def analyze_function_phenotype_match(
        self,
        protein: str,
        functional_data: Optional[Dict], # Made Optional
        phenotype_data: Optional[Dict], # Made Optional
        target_phenotypes: Optional[List[str]], # Made Optional
        literature_data: Optional[Dict], # Made Optional
        tissue_expression_data: Optional[Dict] = None # MODIFIED: Added parameter
    ) -> Dict:
        """Objective function-phenotype analysis for a single protein"""
        logger.info(f"\n{'='*60}")
        logger.info(f"Function-Phenotype Objective Analysis: {protein}")
        logger.info(f"{'='*60}")

        # Defensive coding: ensure inputs are valid dicts/lists or default to empty
        functional_data = functional_data if isinstance(functional_data, dict) else {}
        phenotype_data = phenotype_data if isinstance(phenotype_data, dict) else {}
        literature_data = literature_data if isinstance(literature_data, dict) else {}
        target_phenotypes = target_phenotypes if isinstance(target_phenotypes, list) else []

        if not functional_data: logger.warning(f"{protein}: functional_data is empty or invalid.")
        if not literature_data: logger.warning(f"{protein}: literature_data is empty or invalid.")
        if not target_phenotypes: logger.warning(f"{protein}: target_phenotypes is empty or invalid.")

        result = {
            'protein': protein,
            'evidence_summary': {
                'functional_evidence': {},
                'phenotype_evidence': {},
                'literature_evidence': {},
                'expression_evidence': {} # MODIFIED: Added section
            },
            'match_observations': [],
            'knowledge_gaps': [],
            'data_quality_assessment': {}
        }

        try:
            # Extract functional evidence (database + literature)
            functional_features = self._extract_functional_features(
                functional_data, # Pass the (potentially empty) dict
                literature_data.get('function_articles', [])
            )
            result['evidence_summary']['functional_evidence'] = functional_features
        except Exception as e:
            logger.error(f"{protein}: Failed to extract functional features: {e}", exc_info=True)
            result['evidence_summary']['functional_evidence'] = {'error': str(e)}

        try:
            # Extract phenotype evidence (literature only, CTD removed)
            phenotype_evidence = self._extract_phenotype_evidence(
                phenotype_data, # Pass the (potentially empty) dict
                literature_data.get('interaction_articles', [])
            )
            result['evidence_summary']['phenotype_evidence'] = phenotype_evidence
        except Exception as e:
            logger.error(f"{protein}: Failed to extract phenotype evidence: {e}", exc_info=True)
            result['evidence_summary']['phenotype_evidence'] = {'error': str(e)}

        try:
            # Summarize literature evidence
            literature_summary = self._summarize_literature(literature_data)
            result['evidence_summary']['literature_evidence'] = literature_summary
        except Exception as e:
            logger.error(f"{protein}: Failed to summarize literature: {e}", exc_info=True)
            result['evidence_summary']['literature_evidence'] = {'error': str(e)}
        
        try:
            # MODIFIED: Summarize expression evidence
            result['evidence_summary']['expression_evidence'] = self._summarize_expression(tissue_expression_data)
        except Exception as e:
            logger.error(f"{protein}: Failed to summarize expression data: {e}", exc_info=True)
            result['evidence_summary']['expression_evidence'] = {'error': str(e)}

        # Make objective observations for each target phenotype
        for phenotype in target_phenotypes:
            try:
                phenotype_normalized = self._normalize_phenotype_name(phenotype)
                observation = self._make_objective_observations(
                    protein=protein,
                    functional_features=functional_features, # Use extracted features
                    phenotype_evidence=phenotype_evidence, # Use extracted evidence
                    target_phenotype=phenotype_normalized,
                    literature=literature_summary # Pass literature summary
                )
                result['match_observations'].append(observation)
            except Exception as e:
                logger.error(f"{protein}: Failed observation for {phenotype}: {e}", exc_info=True)
                result['match_observations'].append({
                    'target_phenotype': phenotype,
                    'error': f"Observation generation failed: {e}"
                })

        try:
            # Identify knowledge gaps based on extracted data
            result['knowledge_gaps'] = self._identify_knowledge_gaps(
                functional_features,
                phenotype_evidence,
                literature_summary,
                target_phenotypes,
                result['evidence_summary']['expression_evidence'] # MODIFIED: Pass expression data
            )
        except Exception as e:
            logger.error(f"{protein}: Failed to identify knowledge gaps: {e}", exc_info=True)
            result['knowledge_gaps'] = [f"Error identifying gaps: {e}"]

        try:
            # Assess data quality based on input data presence/counts
            result['data_quality_assessment'] = self._assess_data_quality(
                functional_data,
                phenotype_data, # Keep phenotype_data for potential future use or consistency
                literature_data,
                tissue_expression_data # MODIFIED: Pass expression data
            )
        except Exception as e:
            logger.error(f"{protein}: Failed to assess data quality: {e}", exc_info=True)
            result['data_quality_assessment'] = {'error': str(e)}

        logger.info(f"✅ Objective Analysis Complete for {protein}")
        logger.info(f"   - Observations: {len(result['match_observations'])}")
        logger.info(f"   - Knowledge Gaps: {len(result['knowledge_gaps'])}")

        return result

    # === NEW METHOD - batch_analyze_proteins ===
    async def batch_analyze_proteins(
        self,
        proteins: List[str],
        functional_data_dict: Dict[str, Optional[Dict]], # Dict mapping protein to its data
        phenotype_data_dict: Dict[str, Optional[Dict]], # Dict mapping protein to its data
        literature_data_dict: Dict[str, Optional[Dict]], # Dict mapping protein to its data
        target_phenotypes: List[str],
        max_concurrent: int = 5 # Concurrency limit for analysis (if needed)
    ) -> Dict[str, Dict]:
        """Batch analyze multiple proteins for function-phenotype matching."""
        # NOTE: This method requires 'tissue_expression_data' to be passed from core_engine
        # This implementation assumes it's NOT passed here, but in the single call.
        # If batch_analyze_proteins is used by core_engine, core_engine must be modified
        # to pass 'tissue_expression_data_dict' as well.
        logger.info(f"Starting batch analysis for {len(proteins)} proteins.")
        results = {}
        processed_count = 0

        # Note: Current analyze_function_phenotype_match is synchronous.
        # If it becomes async later, asyncio.Semaphore and gather can be used.
        # For now, process sequentially.
        for protein in proteins:
            try:
                logger.debug(f"Batch processing: {protein} ({processed_count+1}/{len(proteins)})")
                # Call the existing single-protein analysis method
                result = self.analyze_function_phenotype_match(
                    protein=protein,
                    functional_data=functional_data_dict.get(protein), # Get data for this protein
                    phenotype_data=phenotype_data_dict.get(protein), # Get data for this protein
                    target_phenotypes=target_phenotypes,
                    literature_data=literature_data_dict.get(protein), # Get data for this protein
                    # MODIFIED: This batch processor does not receive expression data
                    # The fix in core_engine bypasses this batch method.
                    tissue_expression_data=None 
                )
                results[protein] = result

            except Exception as e:
                logger.error(f"Batch analysis critically failed for {protein}: {e}", exc_info=True)
                # Store error information for this protein
                results[protein] = {
                    'protein': protein,
                    'error': f"Critical analysis failure: {str(e)}",
                    'evidence_summary': {},
                    'match_observations': [],
                    'knowledge_gaps': []
                }
            finally:
                processed_count += 1
                # Optional: Add progress reporting here if needed

        logger.info(f"Batch analysis complete. Processed {processed_count} proteins.")
        return results

    # === NEW METHOD - generate_comparative_summary ===
    def generate_comparative_summary(
        self,
        analysis_results: Dict[str, Dict] # Results from batch_analyze_proteins
    ) -> Dict:
        """Generate a comparative summary across multiple protein analyses."""
        logger.info("Generating comparative summary...")

        summary = {
            'total_proteins_processed': len(analysis_results),
            'successful_analysis_count': 0,
            'failed_analysis_count': 0,
            'average_observations_per_protein': 0,
            'average_gaps_per_protein': 0,
            'proteins_by_data_quality': defaultdict(list), # {'high': [], 'medium': [], 'low': []}
            'common_knowledge_gaps': defaultdict(int), # {gap_text: count}
            'proteins_with_strong_convergence': [] # Proteins showing strong links
        }

        total_observations = 0
        total_gaps = 0
        gap_texts = []

        for protein, result in analysis_results.items():
            if result.get('error'):
                summary['failed_analysis_count'] += 1
                continue

            summary['successful_analysis_count'] += 1

            # Aggregate observations and gaps
            obs_count = len(result.get('match_observations', []))
            gaps_list = result.get('knowledge_gaps', [])
            gaps_count = len(gaps_list)

            total_observations += obs_count
            total_gaps += gaps_count
            gap_texts.extend(gaps_list) # Collect all gap texts

            # Assess data quality for this protein
            quality_level = self._assess_single_protein_quality(result)
            summary['proteins_by_data_quality'][quality_level].append(protein)

            # Identify proteins with strong convergence (example criteria)
            has_strong_convergence = any(
                len(obs.get('convergence_points', [])) >= 2
                for obs in result.get('match_observations', [])
            )
            if has_strong_convergence:
                summary['proteins_with_strong_convergence'].append(protein)

        # Calculate averages
        if summary['successful_analysis_count'] > 0:
            summary['average_observations_per_protein'] = round(total_observations / summary['successful_analysis_count'], 1)
            summary['average_gaps_per_protein'] = round(total_gaps / summary['successful_analysis_count'], 1)

        # Find common knowledge gaps
        for gap in gap_texts:
             if isinstance(gap, str): # Ensure it's a string
                summary['common_knowledge_gaps'][gap] += 1

        # Sort common gaps by frequency
        summary['common_knowledge_gaps'] = dict(sorted(
            summary['common_knowledge_gaps'].items(),
            key=lambda item: item[1],
            reverse=True
        )[:5]) # Keep top 5 common gaps

        logger.info("Comparative summary generated.")
        return summary

    # === NEW METHOD - extract_critical_gaps_for_stage2 ===
    def extract_critical_gaps_for_stage2(
        self,
        analysis_results: Dict[str, Dict], # Results from batch_analyze_proteins
        top_n_proteins: int = 5 # Number of top proteins to focus on for gaps
    ) -> Dict[str, List[str]]:
        """Extract critical knowledge gaps from top proteins needing Stage 2 search."""
        logger.info(f"Extracting critical gaps for top {top_n_proteins} proteins...")
        gaps_by_protein = {}

        # Basic ranking (e.g., by number of convergence points, needs refinement)
        # Placeholder: Sort by data quality first, then maybe observation count
        def rank_protein(item):
            protein, result = item
            quality_map = {'high': 3, 'medium': 2, 'low': 1, 'error': 0}
            quality_score = quality_map.get(self._assess_single_protein_quality(result), 0)
            obs_count = len(result.get('match_observations', []))
            # Prioritize quality, then observations, less gaps is better?
            return (quality_score, obs_count, -len(result.get('knowledge_gaps', [])))

        try:
            # Sort proteins based on the ranking function
            sorted_proteins = sorted(
                 analysis_results.items(),
                 key=rank_protein,
                 reverse=True # Higher score is better
            )
        except Exception as sort_e:
            logger.error(f"Failed to sort proteins for gap extraction: {sort_e}", exc_info=True)
            # Fallback: use first N proteins as they appear
            sorted_proteins = list(analysis_results.items())


        # Extract gaps from the top N proteins that had a successful analysis
        count = 0
        for protein, result in sorted_proteins:
             if count >= top_n_proteins:
                 break
             if not result.get('error'): # Only consider successfully analyzed proteins
                 gaps = result.get('knowledge_gaps', [])
                 # Filter out potential error messages stored in gaps list
                 valid_gaps = [g for g in gaps if isinstance(g, str) and "Error" not in g]
                 if valid_gaps:
                     gaps_by_protein[protein] = valid_gaps
                     count += 1 # Increment count only if valid gaps were found

        logger.info(f"Extracted gaps for {len(gaps_by_protein)} top proteins.")
        return gaps_by_protein

    # === Existing internal helper methods ===
    def _extract_functional_features(
        self,
        functional_data: Dict,
        function_literature: List[Dict] # Literature specific to function
    ) -> Dict:
        """Extract functional features from database results and function-specific literature."""
        features = {
            'database_features': {},
            'literature_features': {}
        }

        # Extract from database data passed in functional_data['functional_data']
        db_functional = functional_data.get('functional_data', {})
        if isinstance(db_functional, dict):
            # KEGG pathways
            kegg = db_functional.get('kegg_pathways', {})
            if isinstance(kegg, dict):
                features['database_features']['pathways'] = kegg.get('pathways', []) # MODIFIED: Fixed key name
                features['database_features']['pathway_categories'] = kegg.get('categorized_pathways', {})

            # UniProt data
            uniprot = db_functional.get('uniprot_functions', {}) # MODIFIED: Fixed key name
            if isinstance(uniprot, dict):
                features['database_features']['molecular_functions'] = uniprot.get('functions', [])
                features['database_features']['binding_sites'] = uniprot.get('binding_sites', [])
        else:
             logger.warning("Database functional data format unexpected or missing.")


        # Extract summary from function-specific literature
        if isinstance(function_literature, list) and function_literature:
            lit_functions = []
            for article in function_literature[:10]: # Limit processing
                if isinstance(article, dict):
                    lit_functions.append({
                        'pmid': article.get('pmid'),
                        'title': article.get('title', 'N/A')[:100], # Truncate long titles
                        'abstract_excerpt': article.get('abstract', '')[:200] + ('...' if len(article.get('abstract', '')) > 200 else ''),
                        'relevance_score': article.get('ai_relevance_score', 0)
                    })
            features['literature_features']['function_articles'] = lit_functions
            features['literature_features']['function_articles_count'] = len(lit_functions)


        return features

    def _extract_phenotype_evidence(
        self,
        phenotype_data: Dict, # Keep for potential future use (e.g., OMIM, ClinVar)
        interaction_literature: List[Dict] # Literature specific to interaction
    ) -> Dict:
        """Extract phenotype evidence (currently only from interaction literature)."""
        evidence = {
            'database_evidence': {}, # Placeholder for future db sources
            'literature_evidence': {}
        }

        # Process interaction literature summary
        if isinstance(interaction_literature, list) and interaction_literature:
            lit_interactions = []
            for article in interaction_literature[:10]: # Limit processing
                if isinstance(article, dict):
                    lit_interactions.append({
                        'pmid': article.get('pmid'),
                        'title': article.get('title', 'N/A')[:100], # Truncate long titles
                        'abstract_excerpt': article.get('abstract', '')[:200] + ('...' if len(article.get('abstract', '')) > 200 else ''),
                        'relevance_score': article.get('ai_relevance_score', 0)
                    })
            evidence['literature_evidence']['interaction_articles'] = lit_interactions
            evidence['literature_evidence']['interaction_articles_count'] = len(lit_interactions)

        return evidence

    def _make_objective_observations(
        self,
        protein: str,
        functional_features: Dict, # Result from _extract_functional_features
        phenotype_evidence: Dict, # Result from _extract_phenotype_evidence
        target_phenotype: str,
        literature: Dict # Result from _summarize_literature
    ) -> Dict:
        """Make objective observations about potential links (no scoring)."""
        observation = {
            'target_phenotype': target_phenotype,
            'functional_observations': [],
            'phenotype_observations': [],
            'convergence_points': [], # Points where function/pathway matches phenotype profile
            'divergence_points': [] # Points where they don't match (less emphasized)
        }

        # Check if we have a known profile for the target phenotype
        if target_phenotype not in self.phenotype_function_map:
            observation['note'] = f"No predefined profile available for {target_phenotype}."
            # Still add literature counts if available
            if literature.get('total_articles', 0) > 0:
                 observation['functional_observations'].append(f"{literature.get('function_articles_count', 0)} function articles found.")
                 observation['phenotype_observations'].append(f"{literature.get('interaction_articles_count', 0)} interaction articles found.")
            return observation

        phenotype_profile = self.phenotype_function_map[target_phenotype]
        expected_functions = {f.lower() for f in phenotype_profile.get('related_functions', [])}
        expected_pathways = {p.lower() for p in phenotype_profile.get('related_pathways', [])}

        # Check functional database features against phenotype profile
        db_features = functional_features.get('database_features', {})
        protein_functions = {str(f).lower() for f in db_features.get('molecular_functions', [])}
        protein_pathways_data = db_features.get('pathways', []) # List of dicts [{'id': ..., 'name': ...}]
        protein_pathway_names = {str(p.get('name', '')).lower() for p in protein_pathways_data if isinstance(p, dict)}


        # Find convergences
        matching_functions = protein_functions.intersection(expected_functions)
        matching_pathways = protein_pathway_names.intersection(expected_pathways)

        if matching_functions:
             observation['convergence_points'].append(
                 f"Protein functions match expected for {target_phenotype}: {', '.join(matching_functions)}"
             )
        if matching_pathways:
             observation['convergence_points'].append(
                 f"Protein pathways match expected for {target_phenotype}: {', '.join(matching_pathways)}"
             )

        # Report literature counts
        func_lit_count = functional_features.get('literature_features', {}).get('function_articles_count', 0)
        int_lit_count = phenotype_evidence.get('literature_evidence', {}).get('interaction_articles_count', 0)
        observation['functional_observations'].append(f"{func_lit_count} function-related articles found.")
        observation['phenotype_observations'].append(f"{int_lit_count} interaction-related articles found.")

        # Optional: Add divergence points (less critical for objective report)
        # e.g., if protein has functions NOT expected for the phenotype

        return observation

    def _identify_knowledge_gaps(
        self,
        functional_features: Dict,
        phenotype_evidence: Dict,
        literature_summary: Dict,
        target_phenotypes: List[str], # Keep for potential future use
        expression_evidence: Dict # MODIFIED: Added parameter
    ) -> List[str]:
        """Identify knowledge gaps based on the presence/absence of data."""
        gaps = []

        # Check functional database information
        db_features = functional_features.get('database_features', {})
        if not db_features.get('pathways'):
            gaps.append("Missing KEGG pathway annotations.")
        if not db_features.get('molecular_functions'):
            gaps.append("Missing UniProt molecular function annotations.")

        # Check literature coverage
        func_lit_count = functional_features.get('literature_features', {}).get('function_articles_count', 0)
        int_lit_count = phenotype_evidence.get('literature_evidence', {}).get('interaction_articles_count', 0)

        if func_lit_count == 0:
            gaps.append("No relevant function-focused literature found.")
        elif func_lit_count < 3:
            gaps.append(f"Limited function-focused literature found ({func_lit_count}).")

        if int_lit_count == 0:
            gaps.append("No relevant interaction-focused literature found (protein-pollutant/phenotype link).")
        elif int_lit_count < 3:
            gaps.append(f"Limited interaction-focused literature found ({int_lit_count}).")

        # Check overall literature relevance
        avg_relevance = literature_summary.get('average_relevance', 0)
        if literature_summary.get('total_articles', 0) > 0 and avg_relevance < 5.0:
            gaps.append(f"Low average relevance score ({avg_relevance:.1f}/10) across found literature.")

        # MODIFIED: Check expression evidence
        if not expression_evidence or not expression_evidence.get('source'):
            gaps.append("Missing HPA/GTEx expression data for human ortholog.")

        # Potential future gap: Missing links to specific target_phenotypes in literature

        return gaps

    def _assess_data_quality(
        self,
        functional_data: Dict,
        phenotype_data: Dict, # Kept for consistency, but not used currently
        literature_data: Dict,
        tissue_expression_data: Optional[Dict] = None # MODIFIED: Added parameter
    ) -> Dict:
        """Assess data quality based on source availability and literature metrics."""

        # Check database coverage using the raw_data structure if available
        raw_data = functional_data.get('raw_data', {})
        db_coverage = {
            'uniprot': bool(raw_data.get('uniprot')),
            'kegg': bool(raw_data.get('kegg'))
            # Add checks for other dbs (GTEx, HPA) if they were queried and stored here
        }
        
        # MODIFIED: Add expression data to coverage check
        db_coverage['hpa'] = False
        db_coverage['gtex'] = False

        if tissue_expression_data and isinstance(tissue_expression_data, dict):
            source = tissue_expression_data.get('source')
            if source == 'HPA':
                db_coverage['hpa'] = True
            elif source == 'GTEx':
                db_coverage['gtex'] = True

        # Assess literature coverage
        articles = literature_data.get('articles', [])
        function_articles = literature_data.get('function_articles', [])
        interaction_articles = literature_data.get('interaction_articles', [])
        lit_coverage = {
            'total_articles': len(articles) if isinstance(articles, list) else 0,
            'function_articles': len(function_articles) if isinstance(function_articles, list) else 0,
            'interaction_articles': len(interaction_articles) if isinstance(interaction_articles, list) else 0
        }

        # Assess literature quality metrics
        lit_quality = {}
        if isinstance(articles, list) and articles:
            scores = [a.get('ai_relevance_score', 0) for a in articles if isinstance(a, dict)]
            high_quality_count = sum(1 for s in scores if s >= 7)
            fulltext_count = sum(1 for a in articles if isinstance(a, dict) and a.get('pmcid')) # Basic check via pmcid

            lit_quality = {
                'average_relevance': round(sum(scores) / len(scores), 1) if scores else 0,
                'high_relevance_count': high_quality_count,
                'high_relevance_ratio': round(high_quality_count / len(articles), 2) if articles else 0,
                'fulltext_available_count': fulltext_count,
                'fulltext_ratio': round(fulltext_count / len(articles), 2) if articles else 0
            }

        return {
            'database_coverage': db_coverage,
            'literature_coverage': lit_coverage,
            'literature_quality': lit_quality
        }

    # === NEW METHOD - _assess_single_protein_quality (Helper for summary) ===
    def _assess_single_protein_quality(self, analysis_result: Dict) -> str:
        """Assess overall data quality for a single protein's analysis result."""
        if not analysis_result or analysis_result.get('error'):
            return 'error' # Mark as error if analysis failed

        quality_assessment = analysis_result.get('data_quality_assessment', {})
        db_coverage = quality_assessment.get('database_coverage', {})
        lit_coverage = quality_assessment.get('literature_coverage', {})
        lit_quality = quality_assessment.get('literature_quality', {})

        # Define criteria for high, medium, low quality
        has_kegg = db_coverage.get('kegg', False)
        has_uniprot = db_coverage.get('uniprot', False)
        # MODIFIED: Add expression check
        has_expression = db_coverage.get('hpa', False) or db_coverage.get('gtex', False)
        
        total_articles = lit_coverage.get('total_articles', 0)
        high_relevance_articles = lit_quality.get('high_relevance_count', 0)

        # High quality: Both key databases + expression + reasonable amount of high-quality literature
        if has_kegg and has_uniprot and has_expression and total_articles >= 5 and high_relevance_articles >= 2:
            return 'high'
        # Medium quality: At least one key database + some literature, OR both databases but poor literature
        elif (has_kegg or has_uniprot) and (has_expression or total_articles >= 2):
            return 'medium'
        elif has_kegg and has_uniprot and has_expression: # All DBs but few/no articles
             return 'medium'
        # Low quality: Missing key database info and limited/no literature
        else:
            return 'low'

    # === NEW METHOD - _summarize_expression (Helper for summary) ===
    def _summarize_expression(self, expr_data: Optional[Dict]) -> Dict:
        """Formats expression data dictionary into a summary."""
        if expr_data and isinstance(expr_data, dict) and expr_data.get('tpm') is not None:
            return {
                'tpm': expr_data.get('tpm', 0),
                'level': expr_data.get('expression_level', 'N/A'),
                'percentile': expr_data.get('percentile', 0),
                'source': expr_data.get('source', 'N/A'),
                'tissue': expr_data.get('tissue', 'N/A')
            }
        return {} # Return empty dict if no data

    def _summarize_literature(self, literature_data: Dict) -> Dict:
        """Summarize basic counts and average relevance from literature data."""
        articles = literature_data.get('articles', [])
        # Ensure articles is a list
        articles = articles if isinstance(articles, list) else []

        function_articles = literature_data.get('function_articles', [])
        interaction_articles = literature_data.get('interaction_articles', [])
        # Ensure these are lists too
        function_articles = function_articles if isinstance(function_articles, list) else []
        interaction_articles = interaction_articles if isinstance(interaction_articles, list) else []


        scores = [a.get('ai_relevance_score', 0) for a in articles if isinstance(a, dict)]
        avg_relevance = round(sum(scores) / len(scores), 1) if scores else 0

        return {
            'total_articles': len(articles),
            'function_articles_count': len(function_articles),
            'interaction_articles_count': len(interaction_articles),
            'average_relevance': avg_relevance
        }

    def _normalize_phenotype_name(self, phenotype: str) -> str:
        """Normalize phenotype name to match internal keys."""
        if not phenotype or not isinstance(phenotype, str):
            return 'unknown'

        phenotype_lower = phenotype.lower().strip()

        # Simple mapping based on keywords
        if 'liver' in phenotype_lower or 'hepat' in phenotype_lower:
            return 'hepatotoxicity'
        elif 'kidney' in phenotype_lower or 'nephr' in phenotype_lower or 'renal' in phenotype_lower:
            return 'nephrotoxicity'
        elif 'brain' in phenotype_lower or 'neuro' in phenotype_lower:
            return 'neurotoxicity'
        elif 'heart' in phenotype_lower or 'cardio' in phenotype_lower:
            return 'cardiotoxicity'
        else:
            # Return lowercase version if no match found
            return phenotype_lower
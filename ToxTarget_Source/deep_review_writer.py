# -*- coding: utf-8 -*-
"""
Deep Review Writer Module
"""

import logging
import re
import json
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

LANGUAGE_CONSTRAINT = "\n\n**CRITICAL: All responses must be in English.**\n"


class DeepReviewWriter:
    """Deep Research Mini-Review Writer"""

    def __init__(self, ai_service, emit_log_callback):
        self.ai_service = ai_service
        self.emit_log = emit_log_callback
        self.valid_pmids = set()
        logger.info("DeepReviewWriter initialized")
        if self.emit_log:
            self.emit_log("Deep Review Writer initialized", 'info')

    def register_valid_pmids(self, literature_dict: Dict):
        """Register valid PMIDs to prevent hallucinations."""
        if not literature_dict:
            return

        for protein, data in literature_dict.items():
            if not isinstance(data, dict):
                continue

            articles = data.get('articles', [])
            for article in articles:
                pmid = article.get('pmid')
                if pmid:
                    self.valid_pmids.add(str(pmid))

        logger.info(f"Registered {len(self.valid_pmids)} valid PMIDs")
        if self.emit_log:
            self.emit_log(f"Registered {len(self.valid_pmids)} valid PMIDs", 'info')

    def _sanitize_citations(self, text: str) -> str:
        """Remove or correct PMIDs not in registered set."""
        if not text: 
            return text

        def replace_pmid(match):
            pmid = match.group(1)
            if pmid in self.valid_pmids:
                return f"(PMID:{pmid})"
            else:
                logger.warning(f"Sanitized hallucinated PMID: {pmid}")
                return "" 

        cleaned_text = re.sub(r'\(?PMID\s*:?\s*(\d{7,8})\)?', replace_pmid, text, flags=re.IGNORECASE)
        cleaned_text = cleaned_text.replace("()", "").replace("  ", " ")
        return cleaned_text

    async def create_detailed_outline(
        self,
        top_proteins: List[Dict],
        critical_extracts: Dict[str, Dict],
        mechanistic_network: Dict,
        inputs: Dict,
        expert_consensus_summary: str = "", 
        database_facts: Dict = None
    ) -> Dict[str, Dict]:
        """Create dynamic hypothesis-driven outline based on expert consensus."""
        if self.emit_log:
            self.emit_log("Creating dynamic hypothesis-driven outline...", 'info')

        pollutant = inputs.get('pollutant', '')
        protein_names = [p.get('protein', '') for p in top_proteins if p.get('protein')]

        system_prompt_content = f"""You are a senior editor at a top toxicology journal. 
Your task is to design the structure of a Review Article synthesizing a multi-stage screening process.
{LANGUAGE_CONSTRAINT}

[GOAL]
Construct a **narrative arc** explaining *how* {pollutant} causes toxicity through specific targets.

[INPUT DATA]
1. **Expert Consensus:** Use the provided PI/Expert logic to structure the review.
2. **Key Proteins:** {', '.join(protein_names)} are the protagonists.

[REQUIRED SECTIONS - STRICT JSON KEYS]
You must generate a JSON object using EXACTLY the following keys. Do not use descriptive titles as keys.
1. "executive_summary": The "Elevator Pitch".
2. "introduction": Clinical problem -> Scientific gap -> Computational approach.
3. "protein_analysis": Detailed subsections for top 1-2 proteins (Mechanistic Core).
4. "mechanistic_model": Systems integration and network view.
5. "evidence_evaluation": Critical analysis and unsolved questions.
6. "validation_strategy": Concrete next steps/roadmap.
7. "clinical_implications": Relevance to human health.
8. "conclusions": Final summary.

[OUTPUT FORMAT]
JSON object with detailed 'key_points', 'required_evidence', and 'suggested_structure' for each section KEY listed above.
Example:
{{
  "executive_summary": {{
      "key_points": ["Point 1", "Point 2"],
      "required_evidence": ["Evidence A"],
      "suggested_structure": "Paragraph 1..."
  }},
  "introduction": {{ ... }}
}}
"""
        
        user_prompt_content = f"""
[Expert Consensus Summary]
{expert_consensus_summary[:3000]} ...

[Top Candidate Proteins]
{json.dumps(top_proteins, indent=2)}

[Task]
Design the outline now using the required JSON keys.
"""

        try:
            outline = await self.ai_service.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=True
            )
            if isinstance(outline, dict) and len(outline) >= 5:
                validated_outline = {}
                
                for section, content in outline.items():
                    norm_section = section.lower().strip()
                    if "executive" in norm_section: norm_section = "executive_summary"
                    elif "introduction" in norm_section: norm_section = "introduction"
                    elif "protein" in norm_section or "mechanistic core" in norm_section: norm_section = "protein_analysis"
                    elif "model" in norm_section or "integration" in norm_section: norm_section = "mechanistic_model"
                    elif "evidence" in norm_section: norm_section = "evidence_evaluation"
                    elif "validation" in norm_section: norm_section = "validation_strategy"
                    elif "clinical" in norm_section: norm_section = "clinical_implications"
                    elif "conclusion" in norm_section: norm_section = "conclusions"

                    if isinstance(content, dict):
                        validated_outline[norm_section] = content
                    else:
                        logger.warning(f"Fixing malformed outline section '{section}' (type: {type(content)})")
                        validated_outline[norm_section] = {
                            "key_points": content if isinstance(content, list) else [str(content)],
                            "required_evidence": [],
                            "suggested_structure": "Standard scientific structure"
                        }
                
                logger.info(f"Outline created with {len(validated_outline)} sections")
                if self.emit_log:
                    self.emit_log(f"Outline created: {len(validated_outline)} sections", 'success')
                return validated_outline
            else:
                logger.warning("Outline structure incomplete, using default")
                return self._get_default_outline()

        except Exception as e:
            logger.error(f"Outline creation failed: {e}", exc_info=True)
            if self.emit_log:
                self.emit_log(f"Outline creation failed: {e}", 'warn')
            return self._get_default_outline()

    def _get_default_outline(self) -> Dict[str, Dict]:
        """Get default outline structure."""
        sections = [
            'executive_summary', 'introduction', 'protein_analysis',
            'mechanistic_model', 'evidence_evaluation', 'validation_strategy',
            'clinical_implications', 'conclusions'
        ]
        return {
            section: {
                'key_points': ['Analysis required'],
                'required_evidence': ['Literature evidence'],
                'suggested_structure': 'Standard scientific writing'
            }
            for section in sections
        }

    def _prepare_evidence_summary(
        self, 
        critical_extracts: Dict[str, Dict], 
        proteins: List[str],
        database_info: Optional[Dict] = None,
        expert_opinions: Optional[str] = None,
        pollutant_context: Optional[str] = None
    ) -> str:
        """Prepare evidence dossier combining Literature, Databases, Expert Opinions AND Pollutant Context."""
        lines = []

        # 0. Pollutant Context
        if pollutant_context:
            lines.append("=== SECTION A: POLLUTANT & PHENOTYPE CONTEXT (Background) ===")
            lines.append(pollutant_context)
            lines.append("")
            lines.append("=== SECTION B: PROTEIN BIOLOGICAL FACTS (Database Ground Truth) ===")
        else:
            lines.append("=== SECTION A: BIOLOGICAL FACTS (Database Ground Truth) ===")

        # 1. Database Facts
        if database_info:
            for prot in proteins:
                if prot in database_info:
                    data = database_info[prot]
                    funcs = data.get('functional_data', {}).get('uniprot_functions', {}).get('functions', [])[:3]
                    paths = data.get('functional_data', {}).get('kegg_pathways', {}).get('pathways', [])
                    path_names = [p.get('pathway_name') for p in paths][:3]
                    expr = data.get('functional_data', {}).get('gtex_expression', {})
                    
                    lines.append(f"PROTEIN [{prot}]:")
                    lines.append(f"  - Normal Function: {'; '.join(funcs)}")
                    lines.append(f"  - Key Pathways: {'; '.join(path_names)}")
                    if expr:
                        lines.append(f"  - Tissue Expression: {expr.get('median_tpm', 'N/A')} TPM in {expr.get('tissue', 'Target')}")
        lines.append("")

        # 2. Literature Evidence
        section_letter = "C" if pollutant_context else "B"
        lines.append(f"=== SECTION {section_letter}: LITERATURE EVIDENCE (Cite these PMIDs) ===")
        if critical_extracts:
            sorted_extracts = sorted(
                critical_extracts.items(), 
                key=lambda x: len(str(x[1])) if x[1] else 0, 
                reverse=True
            )
            
            extract_count = 0
            for pmid, extract in sorted_extracts[:20]:
                if extract_count >= 15: break

                protein_focus = extract.get('protein_focus', '')
                
                title = extract.get('title', '')[:100]
                core_findings = extract.get('core_findings', [])
                func_link = extract.get('functional_link_to_phenotype', [])
                pheno_results = extract.get('key_phenotype_results_data', [])
                
                all_findings = []
                if isinstance(core_findings, list): all_findings.extend(core_findings)
                if isinstance(func_link, list): all_findings.extend(func_link)
                if isinstance(pheno_results, list): all_findings.extend(pheno_results)
                
                all_findings = list(set([f for f in all_findings if f and len(f) > 10]))[:4]

                lines.append(f"SOURCE_ID [PMID:{pmid}]")
                lines.append(f"  Title: {title}")
                if protein_focus:
                    lines.append(f"  Focus Protein: {protein_focus}")
                for finding in all_findings:
                    lines.append(f"  - Evidence: {finding}")
                lines.append("")
                extract_count += 1
        else:
            lines.append("No critical literature extracted.")
        
        # 3. Expert Insights
        section_letter = "D" if pollutant_context else "C"
        lines.append("")
        lines.append(f"=== SECTION {section_letter}: EXPERT PANEL INSIGHTS (For Reasoning, NOT Citation) ===")
        if expert_opinions:
            lines.append(expert_opinions)
        
        return '\n'.join(lines)

    def _get_section_guidelines(self, section_name: str) -> Dict[str, str]:
        """Return adaptive word count and style guidelines based on section type."""
        name = section_name.lower()
        
        if 'executive' in name or 'summary' in name:
            return {
                "length": "300-500 words",
                "style": "Concise, high-level overview. Focus on the 'Big Picture'."
            }
        elif 'introduction' in name:
            return {
                "length": "600-800 words",
                "style": "Comprehensive background. Set the stage for the clinical/scientific problem."
            }
        elif any(x in name for x in ['mechanistic', 'protein', 'core', 'analysis', 'systems']):
            return {
                "length": "1000-1500 words",
                "style": "Exhaustive, deep, and detailed. This is the core of the review. Use causal reasoning to connect binding -> function -> phenotype."
            }
        elif any(x in name for x in ['validation', 'roadmap', 'plan', 'implication']):
            return {
                "length": "600-800 words",
                "style": "Actionable and specific. Propose concrete experiments."
            }
        elif 'conclusion' in name:
            return {
                "length": "300-500 words",
                "style": "Impactful summary. Reiterate key findings and limitations."
            }
        else:
            return {
                "length": "500-800 words",
                "style": "Standard scientific review depth."
            }

    async def write_section(
        self,
        section_name: str,
        outline_section: Any,
        critical_extracts: Dict[str, Dict],
        context_data: Dict,
        full_evidence_dossier: str = ""
    ) -> str:
        """Write individual section content with strict citation enforcement and adaptive length."""
        if self.emit_log:
            self.emit_log(f"Writing section: {section_name}", 'info')

        if not full_evidence_dossier:
             pollutant = context_data.get('pollutant', '')
             proteins = context_data.get('proteins', [])
             pollutant_context = context_data.get('pollutant_context', '')
             full_evidence_dossier = self._prepare_evidence_summary(
                 critical_extracts, 
                 proteins, 
                 pollutant_context=pollutant_context
             )
        
        key_points = []
        structure = ""
        
        if isinstance(outline_section, dict):
            key_points = outline_section.get('key_points', [])
            structure = outline_section.get('suggested_structure', '')
        else:
            logger.warning(f"Section '{section_name}' has invalid outline format ({type(outline_section)}). Using fallback.")
            if isinstance(outline_section, list):
                key_points = outline_section
            elif isinstance(outline_section, str):
                key_points = [outline_section]
            else:
                key_points = ["Content generation required based on title."]
            structure = "Standard scientific review structure."
        
        guidelines = self._get_section_guidelines(section_name)

        # Determine correct section letter for citations based on presence of Pollutant Context
        lit_section_letter = "C" if "SECTION A: POLLUTANT" in full_evidence_dossier else "B"

        system_prompt_content = f"""Write the '{section_name.replace('_', ' ').title()}' section of a high-impact scientific review.
{LANGUAGE_CONSTRAINT}

[WRITING STYLE: THE "PROFESSOR" PERSONA]
1. **Mechanistic, Not Descriptive:** Use causal reasoning (e.g., "Pollutant X binding to Protein A likely inhibits Pathway Y...").
2. **Integrate Evidence:** Combine "Biological Facts" with "Literature Evidence".
3. **Acknowledge Uncertainty:** If Expert Panel raised doubts, mention them.
4. **{guidelines['style']}**

[CITATION RULES - STRICT]
1. **ONLY use citations provided in SECTION {lit_section_letter} (Literature Evidence).**
2. Format: (PMID:12345678).
3. **Pollutant Context (Section A)** can be cited if PMIDs are provided there.
4. Database facts and Expert opinions do NOT get PMID citations unless explicitly provided.
5. **NO HALLUCINATIONS.**

[Target Length] {guidelines['length']}.
Prioritize **DEPTH** over brevity for this section.
"""
        
        user_prompt_content = f"""
[Section Goal]
{'; '.join(map(str, key_points))}

[Structure Guidance]
{structure}

[FULL EVIDENCE DOSSIER]
{full_evidence_dossier}

[Action]
Write the section content now. Ensure you meet the target length of {guidelines['length']}.
"""

        try:
            section_content = await self.ai_service.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=False
            )

            if isinstance(section_content, str) and len(section_content.split()) > 50:
                cleaned_content = self._sanitize_citations(section_content)
                return cleaned_content
            else:
                logger.warning(f"Section '{section_name}' content too short")
                return f"[{section_name.replace('_', ' ').title()} section requires additional content]"

        except Exception as e:
            logger.error(f"Section writing failed for '{section_name}': {e}", exc_info=True)
            if self.emit_log:
                self.emit_log(f"Section '{section_name}' writing failed: {e}", 'error')
            return f"[Error generating {section_name} section]"

    async def integrate_sections(self, sections: Dict[str, str], outline: Dict) -> str:
        """Integrate all sections into coherent document."""
        if self.emit_log:
            self.emit_log("Integrating sections...", 'info')

        section_order = [
            'executive_summary', 'introduction', 'protein_analysis',
            'mechanistic_model', 'evidence_evaluation', 'validation_strategy',
            'clinical_implications', 'conclusions'
        ]

        integrated_parts = []
        for section_name in section_order:
            content = sections.get(section_name, '')
            if content and not content.startswith('[Error') and not content.startswith('[Not generated]'):
                title = section_name.replace('_', ' ').title()
                integrated_parts.append(f"\n## {title}\n\n{content}\n")

        integrated_text = '\n'.join(integrated_parts)

        logger.info(f"Sections integrated successfully: {len(integrated_text.split())} words")
        return integrated_text

    async def critical_review(self, integrated_text: str) -> Dict:
        """Perform critical review of draft."""
        if self.emit_log:
            self.emit_log("Performing critical review...", 'info')

        system_prompt_content = f"""Critically review this mini-review draft.
{LANGUAGE_CONSTRAINT}
[Review Criteria]
Accuracy, Evidence Quality, Flow, Clarity, Completeness.

[Required JSON Output]
{{
  "overall_quality": "excellent / good / adequate / needs_improvement",
  "strengths": ["Strength 1"],
  "weaknesses": ["Weakness 1"],
  "specific_improvements": [
    {{"section": "name", "issue": "desc", "suggestion": "fix"}}
  ],
  "citation_issues": [],
  "scientific_accuracy_score": 8
}}"""
        
        user_prompt_content = f"""
[Draft Text (first 8000 chars)]
{integrated_text[:8000]}
"""

        try:
            review = await self.ai_service.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=True
            )
            if isinstance(review, dict):
                return review
            else:
                return self._get_default_review()

        except Exception as e:
            logger.error(f"Critical review failed: {e}", exc_info=True)
            return self._get_default_review()

    def _get_default_review(self) -> Dict:
        return {
            'overall_quality': 'adequate',
            'strengths': ['Analysis attempt'],
            'weaknesses': ['Refinement needed'],
            'specific_improvements': [],
            'citation_issues': [],
            'scientific_accuracy_score': 7
        }

    async def apply_improvements(self, integrated_text: str, review: Dict, full_evidence_dossier: str = "") -> str:
        """Apply improvements preserving citations and preventing hallucinations."""
        if self.emit_log:
            self.emit_log("Applying improvements...", 'info')

        improvements = review.get('specific_improvements', [])
        if not improvements:
            return integrated_text

        improvement_list = []
        for idx, imp in enumerate(improvements[:5], 1):
            improvement_list.append(f"{idx}. Section: {imp.get('section')}\n   Issue: {imp.get('issue')}\n   Suggestion: {imp.get('suggestion')}")

        system_prompt_content = f"""Apply improvements to this text.
{LANGUAGE_CONSTRAINT}
[CRITICAL RULES]
1. **PRESERVE ALL EXISTING CITATIONS (PMID:xxxx) EXACTLY.**
2. Keep scientific accuracy.
3. **DO NOT HALLUCINATE:** Only use information provided in the [FULL EVIDENCE DOSSIER]. If information is missing, acknowledge the limitation rather than inventing facts.
"""
        
        evidence_section = ""
        if full_evidence_dossier:
            evidence_section = f"\n[FULL EVIDENCE DOSSIER]\n{full_evidence_dossier[:15000]}...\n"

        user_prompt_content = f"""
[Draft Text]
{integrated_text[:10000]}

[Improvements to Apply]
{chr(10).join(improvement_list)}
{evidence_section}
"""

        try:
            improved = await self.ai_service.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=False
            )
            if isinstance(improved, str) and len(improved.split()) > 500:
                return self._sanitize_citations(improved)
            else:
                return integrated_text

        except Exception as e:
            logger.error(f"Improvement failed: {e}", exc_info=True)
            return integrated_text
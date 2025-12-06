# -*- coding: utf-8 -*-
"""
Expert Meeting Manager - Manage multi-round expert discussions
Extended version with ExpertSystemHelper for prompt management.
"""

import logging
import asyncio
import re
import statistics
from typing import Dict, List, Callable, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

LANGUAGE_CONSTRAINT = "\n\n**CRITICAL: All responses must be in English.**\n"

class ExpertMeetingManager:
    """
    Expert Meeting Manager - Manages multi-round expert discussion flow
    """

    def __init__(self, ai_service, emit_log_callback=None):
        """Initialize meeting manager"""
        self.ai_service = ai_service
        self.emit_log = emit_log_callback or self._default_log
        self.discussion_history = []
        self.expert_helper = ExpertSystemHelper()

    def _default_log(self, message: str, level: str = 'info'):
        """Default log function"""
        logger.info(f"[{level.upper()}] {message}")

    async def run_round_discussion(
        self,
        round_name: str,
        experts: List[str],
        get_expert_prompt_func: Callable,
        pi_name: str,
        agenda: str,
        context_data: Dict,
        num_discussion_rounds: int = 2,
        synthesis_between_rounds: bool = True
    ) -> Dict:
        """Run complete expert discussion meeting"""

        round_discussion_history = []

        self.emit_log(f"\n{'='*70}", 'info')
        self.emit_log(f"{round_name} - Expert Panel Discussion", 'info')
        self.emit_log(f"{'='*70}\n", 'info')

        self.emit_log(f"{pi_name} - Opening Remarks", 'info')
        pi_opening = await self._pi_opening_speech(
            pi_name=pi_name,
            agenda=agenda,
            experts=experts,
            context_data=context_data
        )
        round_discussion_history.append(self._create_speech_entry(pi_name, pi_opening, "Opening"))
        self._emit_expert_speech(pi_name, pi_opening)

        for round_idx in range(num_discussion_rounds):
            round_num = round_idx + 1

            self.emit_log(f"\n{'─'*70}", 'info')
            self.emit_log(f"Discussion Round {round_num}/{num_discussion_rounds}", 'info')
            self.emit_log(f"{'─'*70}\n", 'info')

            for expert in experts:
                if expert == pi_name:
                    continue

                speech = await self._expert_speech(
                    expert_name=expert,
                    get_prompt_func=get_expert_prompt_func,
                    context_data=context_data,
                    round_num=round_num,
                    total_rounds=num_discussion_rounds,
                    history=round_discussion_history
                )

                round_discussion_history.append(self._create_speech_entry(expert, speech, f"Round {round_num}"))
                self._emit_expert_speech(expert, speech)

                if "pass" in speech.lower()[:20]:
                    self.emit_log(f"  {expert} has no additional input this turn.", 'info')

            if synthesis_between_rounds and round_idx < num_discussion_rounds - 1:
                self.emit_log(f"\n{pi_name} - Synthesis (Round {round_num})", 'info')
                synthesis = await self._pi_synthesis(
                    pi_name=pi_name,
                    round_num=round_num,
                    total_rounds=num_discussion_rounds,
                    agenda=agenda,
                    history=round_discussion_history
                )
                round_discussion_history.append(self._create_speech_entry(pi_name, synthesis, f"Synthesis R{round_num}"))
                self._emit_expert_speech(pi_name, synthesis, is_synthesis=True)

        self.emit_log(f"\n{'='*70}", 'success')
        self.emit_log(f"{pi_name} - Final Summary (Generating JSON)", 'success')
        self.emit_log(f"{'='*70}\n", 'success')

        final_summary_json = await self._pi_final_summary(
            pi_name=pi_name,
            agenda=agenda,
            context_data=context_data,
            history=round_discussion_history
        )

        final_summary_text = final_summary_json.get(
            'final_summary_text', 
            'PI final summary text could not be extracted from JSON.'
        )

        round_discussion_history.append(self._create_speech_entry(pi_name, final_summary_text, "Final Summary Text"))
        self._emit_expert_speech(pi_name, final_summary_text, is_final=True)

        structured_results = {
            "final_ranking": final_summary_json.get("final_ranking", []),
            "research_plans": final_summary_json.get("research_plans", [])
        }
        
        if not structured_results["final_ranking"]:
            self.emit_log("Warning: PI Final Summary JSON did not contain 'final_ranking'.", 'warn')
        if not structured_results["research_plans"]:
            self.emit_log("Warning: PI Final Summary JSON did not contain 'research_plans'.", 'warn')
        
        self.discussion_history.extend(round_discussion_history)

        return {
            'discussion_history': round_discussion_history,
            'final_summary': final_summary_text, 
            'structured_results': structured_results, 
            'timestamp': datetime.now().isoformat()
        }

    def _calculate_borda_scores(
        self, 
        expert_rankings: Dict[str, List[Dict]], 
        all_proteins: List[str],
        lead_expert_role: Optional[str] = None, 
        weight_factor: float = 1.5
    ) -> List[Dict]:
        """
        Calculates Borda scores based on expert rankings.
        Returns a list of dicts, sorted by score.
        """
        num_proteins = len(all_proteins)
        if num_proteins == 0:
            return []
            
        protein_scores = {protein: 0 for protein in all_proteins}
        protein_ranks = {protein: [] for protein in all_proteins}
        expert_names = list(expert_rankings.keys())

        for expert_name in expert_names:
            ranking_list = expert_rankings.get(expert_name, [])
            if not isinstance(ranking_list, list):
                self.emit_log(f"Warning: Invalid ranking format from {expert_name}. Skipping Borda calc.", 'warn')
                continue
            
            rank_map = {item.get('protein'): item.get('rank') for item in ranking_list if item.get('protein') and isinstance(item.get('rank'), int)}

            weight = 1.0
            if lead_expert_role and lead_expert_role.lower() in expert_name.lower():
                weight = weight_factor

            for protein in all_proteins:
                rank = rank_map.get(protein)
                
                if rank is not None:
                    score = (num_proteins - rank) + 1
                    protein_scores[protein] += (score * weight)
                    protein_ranks[protein].append(rank)
                else:
                    protein_scores[protein] += 0
                    protein_ranks[protein].append(None) 

        borda_results = []
        for protein in all_proteins:
            ranks_for_variance = [r for r in protein_ranks[protein] if r is not None]
            variance = 0.0
            if len(ranks_for_variance) > 1:
                variance = statistics.variance(ranks_for_variance)

            borda_results.append({
                "protein": protein,
                "total_score": protein_scores[protein],
                "ranks_by_expert": protein_ranks[protein],
                "rank_variance": variance
            })
            
        sorted_results = sorted(borda_results, key=lambda x: x['total_score'], reverse=True)
        
        if sorted_results:
            self.emit_log(f"Borda scores calculated. Top protein: {sorted_results[0]['protein']} (Score: {sorted_results[0]['total_score']})", 'success')
        
        return sorted_results

    async def run_round1_discussion(
        self,
        experts: List[str],
        context_data: Dict,
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str],
        valid_proteins: List[str]
    ) -> Dict:
        """
        Run Round 1 discussion using the new (Nomination-based) ExpertSystemHelper flow.
        Flow: PI Opening -> Experts Nominate (Parallel) -> PI Synthesizes Nominations -> Critic Asks Questions
        """
        self.discussion_history = []
        pi_name = "PI (Principal Investigator)"
        critic_name = "Scientific Critic"
        other_experts = [e for e in experts if e != pi_name and e != critic_name]

        self.emit_log(f"\n{'='*70}", 'info')
        self.emit_log("Round 1 Expert Discussion (Nomination Flow)", 'info')
        self.emit_log(f"{'='*70}\n", 'info')

        self.emit_log(f"{pi_name} - Opening Remarks", 'info')
        agenda = self.expert_helper.format_round1_agenda(
            pollutant=pollutant,
            target_tissue=target_tissue,
            target_phenotypes=target_phenotypes,
            valid_proteins=valid_proteins
        )
        pi_opening = await self._pi_opening_speech(
            pi_name=pi_name,
            agenda=agenda,
            experts=experts,
            context_data=context_data
        )
        self._record_speech(pi_name, pi_opening, "Opening")
        self._emit_expert_speech(pi_name, pi_opening)

        self.emit_log(f"\n{'─'*70}", 'info')
        self.emit_log(f"Experts are evaluating and nominating top proteins...", 'info')
        self.emit_log(f"{'─'*70}\n", 'info')

        expert_tasks = []
        for expert in other_experts:
            prompt = self.expert_helper.create_round1_nomination_prompt(
                expert_name=expert,
                context_data=context_data,
                valid_proteins=valid_proteins,
                pollutant=pollutant,
                target_tissue=target_tissue,
                target_phenotypes=target_phenotypes
            )
            expert_tasks.append(self._generate_expert_json(expert, prompt, f"Nominating top 3-5 proteins"))

        expert_results = await asyncio.gather(*expert_tasks, return_exceptions=True)

        expert_nominations_collated = {} 
        expert_nominations_raw_log = {}

        for expert_name, result in zip(other_experts, expert_results):
            if isinstance(result, Exception):
                self.emit_log(f"Expert {expert_name} failed to nominate: {result}", 'error')
                expert_nominations_raw_log[expert_name] = []
            else:
                nominations_list = result.get('nominations', []) 
                expert_nominations_raw_log[expert_name] = nominations_list
                
                log_message_parts = [f"Received {len(nominations_list)} nominations from {expert_name}:"]
                for item in nominations_list: 
                    if isinstance(item, dict):
                        protein = item.get('protein', 'N/A')
                        rationale = item.get('rationale', 'NO RATIONALE PROVIDED')
                        log_message_parts.append(f"  - {protein}: {rationale}")
                        if protein not in expert_nominations_collated:
                            expert_nominations_collated[protein] = []
                        expert_nominations_collated[protein].append({
                            "expert": expert_name,
                            "rationale": rationale
                        })
                
                log_message = "\n".join(log_message_parts)
                self._record_speech(expert_name, log_message, "Nominations") 
                self.emit_log(log_message, 'info') 
        
        self.emit_log(f"\n{pi_name} - Collating Nominations", 'info')
        pi_synthesis_prompt = self.expert_helper.create_pi_nomination_synthesis_prompt(
            expert_nominations_collated
        )
        pi_synthesis_json = await self._generate_expert_json(pi_name, pi_synthesis_prompt, "PI Nomination Synthesis")
        
        high_consensus_list = pi_synthesis_json.get('high_consensus', [])
        specialist_list = pi_synthesis_json.get('specialist_interest', [])
        
        if not isinstance(high_consensus_list, list): high_consensus_list = []
        if not isinstance(specialist_list, list): specialist_list = []

        pi_summary_ranking = [p['protein'] for p in high_consensus_list if p.get('protein')] + \
                             [p['protein'] for p in specialist_list if p.get('protein')]
        
        if not pi_summary_ranking and expert_nominations_collated:
             self.emit_log("PI AI failed to return synthesis, falling back to simple nomination list.", 'warn')
             pi_summary_ranking = list(expert_nominations_collated.keys())
        
        def format_expert_list(experts_field: Any) -> str:
            if isinstance(experts_field, list):
                return ', '.join(experts_field)
            if isinstance(experts_field, str):
                return experts_field
            return 'N/A'
        
        pi_synthesis_text_parts = ["Based on expert nominations, I have collated the following groups:"]
        pi_synthesis_text_parts.append("\n**High Consensus Proteins (Nominated by >1 Expert):**")
        for item in high_consensus_list:
            formatted_experts = format_expert_list(item.get('nominated_by', []))
            pi_synthesis_text_parts.append(f"- **{item.get('protein', 'N/A')}** (Nominated by: {formatted_experts})")
        
        pi_synthesis_text_parts.append("\n**Specialist Interest Proteins (Nominated by 1 Expert):**")
        for item in specialist_list:
             formatted_experts = format_expert_list(item.get('nominated_by', 'N/A'))
             pi_synthesis_text_parts.append(f"- **{item.get('protein', 'N/A')}** (Nominated by: {formatted_experts})")
        
        pi_synthesis_text = "\n".join(pi_synthesis_text_parts)
        
        self._record_speech(pi_name, pi_synthesis_text, "Synthesis")
        self._emit_expert_speech(pi_name, pi_synthesis_text, is_synthesis=True)
        
        self.emit_log(f"PI preliminary ranking (Top {len(pi_summary_ranking)}): {', '.join(pi_summary_ranking)}...", 'success')

        self.emit_log(f"\n{critic_name} - Reviewing Nominations", 'info')
        critic_prompt = self.expert_helper.create_critic_questions_prompt(
            pi_synthesis_json 
        )
        critic_json = await self._generate_expert_json(critic_name, critic_prompt, "Critic Questions")

        critic_questions = critic_json.get('critic_questions', [])
        critic_speech = f"I have reviewed the nominations. I have {len(critic_questions)} critical questions for the team to address in Round 2:\n- " + "\n- ".join(critic_questions)
        
        self._record_speech(critic_name, critic_speech, "Criticism")
        self._emit_expert_speech(critic_name, critic_speech)
        self.emit_log(f"Critic identified {len(critic_questions)} questions for Round 2.", 'success')

        return {
            'discussion_history': self.discussion_history,
            'expert_rankings': expert_nominations_raw_log, 
            'pi_summary_ranking': pi_summary_ranking, 
            'critic_questions': critic_questions,
            'timestamp': datetime.now().isoformat()
        }

    async def _generate_expert_json(self, expert_name: str, prompt: str, task_name: str) -> Dict:
        """Helper to call AI for a JSON response and handle errors."""
        try:
            system_prompt_content = f"You are a helpful scientific expert assistant. {LANGUAGE_CONSTRAINT}"
            user_prompt_content = prompt
            
            response = await self.ai_service.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=True
            )
            if not isinstance(response, dict):
                raise ValueError("AI response was not a dictionary.")
            return response
        except Exception as e:
            self.emit_log(f"Failed to get response from {expert_name} for {task_name}: {e}", 'error')
            return {"error": str(e)}

    async def run_round2_discussion(
        self,
        experts: List[str],
        context_data: Dict,
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str],
        proteins_summary: List[str]
    ) -> Dict:
        """Run Round 2 discussion using ExpertSystemHelper"""

        agenda = self.expert_helper.format_round2_agenda(
            pollutant=pollutant,
            target_tissue=target_tissue,
            target_phenotypes=target_phenotypes,
            proteins_summary=proteins_summary,
            round1_ranking=context_data.get('round1_ranking', []),
            critic_questions=context_data.get('critic_questions', [])
        )

        def get_expert_prompt(expert_name, context_data, recent_discussion, round_num, total_rounds):
            return self.expert_helper.create_round2_expert_prompt(
                expert_name=expert_name,
                context_data=context_data,
                recent_discussion=recent_discussion,
                round_num=round_num,
                total_rounds=total_rounds,
                proteins_summary=proteins_summary,
                pollutant=pollutant,
                target_tissue=target_tissue,
                target_phenotypes=target_phenotypes
            )

        return await self.run_round_discussion(
            round_name="Round 2 Final Evaluation",
            experts=experts,
            get_expert_prompt_func=get_expert_prompt,
            pi_name="PI (Principal Investigator)",
            agenda=agenda,
            context_data=context_data,
            num_discussion_rounds=1,
            synthesis_between_rounds=False
        )


    async def _pi_opening_speech(
        self,
        pi_name: str,
        agenda: str,
        experts: List[str],
        context_data: Dict
    ) -> str:
        """PI opening remarks"""
        expert_list = ", ".join([e for e in experts if e != pi_name])
        context_summary = self._summarize_context(context_data)
        
        try:
            detailed_evidence_text = self.expert_helper._format_evidence_for_prompt(
                context_data.get('proteins_evidence', {})
            )
        except AttributeError:
             detailed_evidence_text = "Detailed evidence summaries are not available for display."

        system_prompt_content = f"""As the {pi_name}, your *primary task* is to set the stage. Welcome the team and state the agenda.
Then, you **MUST** introduce the key candidate proteins by **DIRECTLY quoting** the factual baseline from the '[Detailed Evidence for Proteins]' section.
For each protein, you **MUST** state:
1.  Its **Known Function** (KEGG/UniProt) *exactly as provided*.
2.  Its **Tissue Expression** *exactly as provided*.
3.  Its **Literature Hint** *exactly as provided*.

**CRITICAL RULE 1: DO NOT SUMMARIZE or INVENT information.** If the evidence says "1 UniProt function, 1 KEGG pathway", you must say that. You must use the provided data as your source of truth.
**CRITICAL RULE 2: DO NOT use '...' to truncate lists.** You MUST quote all provided pathways and functions (up to at least 5-10 items if the list is long). Failure to list the data as provided will result in an error.
Only *after* providing this factual baseline should you pose your guiding questions.
{LANGUAGE_CONSTRAINT}"""

        user_prompt_content = f"""
[Meeting Participants]
{expert_list}

[Agenda]
{agenda}

[Available Data Summary]
{context_summary}

[Detailed Evidence for Proteins (Quote this data exactly)]
{detailed_evidence_text}
"""
        return await self.ai_service.generate(
            system_prompt=system_prompt_content,
            user_prompt=user_prompt_content,
            is_json=False
        )

    async def _expert_speech(
        self,
        expert_name: str,
        get_prompt_func: Callable,
        context_data: Dict,
        round_num: int,
        total_rounds: int,
        history: List[Dict]
    ) -> str:
        """Individual expert speech using the provided prompt function"""
        recent_discussion = self._format_recent_discussion(history, last_n=5)

        prompt = get_prompt_func(
            expert_name=expert_name,
            context_data=context_data,
            recent_discussion=recent_discussion,
            round_num=round_num,
            total_rounds=total_rounds
        )

        if LANGUAGE_CONSTRAINT not in prompt:
            prompt += LANGUAGE_CONSTRAINT
        
        system_prompt_content = f"You are a helpful scientific expert assistant. {LANGUAGE_CONSTRAINT}"
        user_prompt_content = prompt

        return await self.ai_service.generate(
            system_prompt=system_prompt_content,
            user_prompt=user_prompt_content,
            is_json=False
        )

    async def _pi_synthesis(
        self,
        pi_name: str,
        round_num: int,
        total_rounds: int,
        agenda: str,
        history: List[Dict]
    ) -> str:
        """PI synthesis of current round"""
        recent_discussion = self._format_recent_discussion(history, last_n=10)

        system_prompt_content = f"""As the {pi_name}, synthesize the key points discussed in Round {round_num}.
{LANGUAGE_CONSTRAINT}
Your synthesis should:
1. Briefly summarize the main contributions or viewpoints expressed by experts in this round.
2. Identify key points of emerging consensus, if any.
3. Highlight significant disagreements or unanswered questions raised in this round.
4. Relate the discussion back to the original agenda points.
5. Pose 1-2 focused questions to guide the next round ({round_num + 1}), aiming to resolve disagreements or explore key points further.
Keep the synthesis focused and concise (target 2-3 paragraphs)."""

        user_prompt_content = f"""
[Original Agenda]
{agenda}

[Recent Discussion (Round {round_num})]
{recent_discussion}
"""
        return await self.ai_service.generate(
            system_prompt=system_prompt_content,
            user_prompt=user_prompt_content,
            is_json=False
        )

    async def _pi_final_summary(
        self,
        pi_name: str,
        agenda: str,
        context_data: Dict,
        history: List[Dict]
    ) -> Dict: 
        """PI final summary (structured JSON)"""
        full_discussion = self._format_recent_discussion(history, last_n=999)

        system_prompt_content = self.expert_helper.create_pi_final_ranking_and_plan_prompt(
            pi_name=pi_name
        )

        user_prompt_content = f"""
[Original Agenda]
{agenda}

[Complete Discussion History]
{full_discussion}

[Available Data Summary]
{self._summarize_context(context_data)}
"""
        result_json = await self.ai_service.generate(
            system_prompt=system_prompt_content,
            user_prompt=user_prompt_content,
            is_json=True 
        )
        
        if not isinstance(result_json, dict):
            self.emit_log(f"PI Final Summary returned non-dict type: {type(result_json)}. Defaulting.", 'error')
            return {"final_ranking": [], "research_plans": [], "final_summary_text": "Error: AI failed to return valid JSON summary."}
        
        return result_json

    async def _extract_structured_results(
        self,
        final_summary: str,
        round_name: str
    ) -> Dict:
        """Extract structured data from the final text summary (R2 optimized)."""
        
        results = {
            "final_ranking": [],
            "research_plans": []
        }

        try:
            ranking_match = re.search(
                r'### 1. Final Identified Proteins & Mechanistic Hypothesis\s*([\s\S]*?)\s*(?:###|$)',
                final_summary,
                re.IGNORECASE
            )
            if ranking_match:
                ranking_text = ranking_match.group(1)
                ranking_list = re.findall(
                    r'\*\*(?P<protein1>[A-Za-z0-9_-]+)\*\*|^\s*(?:\*|-)\s*\*\*(?P<protein2>[A-Za-z0-9_-]+)\*\*',
                    ranking_text,
                    re.MULTILINE
                )
                flat_list = [item for t in ranking_list for item in t if item] 
                results["final_ranking"] = list(dict.fromkeys(flat_list)) 
            else:
                self.emit_log("Could not parse '### 1. Final Identified Proteins' section.", 'warn')

            plans_match = re.search(
                r'### 2. Experimental Validation Plan\s*([\s\S]*?)\s*(?:###|$)',
                final_summary,
                re.IGNORECASE
            )
            if plans_match:
                plans_text = plans_match.group(1)
                plan_splits = re.split(
                    r'^\s*(?:\*|-)\s*\*\*(?:For\s*)?([A-Za-z0-9_-]+):\*\*',
                    plans_text,
                    flags=re.IGNORECASE | re.MULTILINE
                )
                
                if len(plan_splits) > 1:
                    for i in range(1, len(plan_splits), 2):
                        protein_name = plan_splits[i].strip()
                        plan_description = plan_splits[i+1].strip()
                        results["research_plans"].append({
                            "protein": protein_name,
                            "plan": plan_description
                        })
            else:
                 self.emit_log("Could not parse '### 2. Experimental Validation Plan' section.", 'warn')
            
            if not results["final_ranking"] and "Round 1" in round_name:
                pass

        except Exception as e:
            logger.error(f"Error during structured result extraction: {e}")
            self.emit_log(f"Error parsing final summary: {e}", 'error')

        return results



    def _create_speech_entry(self, speaker: str, content: str, stage: str) -> Dict:
        """Create a new speech entry dictionary."""
        return {
            'speaker': speaker,
            'content': content,
            'stage': stage,
            'timestamp': datetime.now().isoformat()
        }

    def _record_speech(self, speaker: str, content: str, stage: str):
        """Record speech to the instance's main history"""
        entry = self._create_speech_entry(speaker, content, stage)
        self.discussion_history.append(entry)

    def _emit_expert_speech(
        self,
        speaker: str,
        content: str,
        is_synthesis: bool = False,
        is_final: bool = False
    ):
        """Send expert speech to UI/log callback"""
        log_level = 'success' if is_final else 'info'
        prefix = ""
        if is_final:
            prefix = "(Final Summary)"
        elif is_synthesis:
            prefix = "(Synthesis)"

        message = f"{speaker} {prefix}:\n{content}"
        self.emit_log(message, log_level)

    def _format_recent_discussion(self, history: List[Dict], last_n: int = 5) -> str:
        """Format recent discussion records for prompt context"""
        if not history:
            return "No prior discussion recorded yet."

        recent_entries = history[-last_n:]

        formatted = []
        for entry in recent_entries:
            speaker = entry.get('speaker', 'Unknown')
            stage = entry.get('stage', 'Unknown')
            content = entry.get('content', '...')
            
            if entry.get('stage') in ['Opening', 'Synthesis', 'Criticism', 'Rank Synthesis', 'Final Summary', 'Final Summary Text']:
                formatted.append(f"[{speaker} - {stage}]: {content}") 
            else:
                formatted.append(f"[{speaker} - {stage}]: {content[:300]}...") 

        return "\n".join(formatted)

    def _summarize_context(self, context_data: Dict) -> str:
        """Summarize available context data"""
        summary_parts = []

        if 'database_analysis' in context_data and isinstance(context_data['database_analysis'], dict):
            count = len(context_data['database_analysis'])
            summary_parts.append(f"- Database Annotations: Data available for {count} proteins (KEGG, UniProt).")

        if 'tissue_expression' in context_data and isinstance(context_data['tissue_expression'], dict):
            count = len(context_data['tissue_expression'])
            summary_parts.append(f"- Tissue Expression: Data available for {count} proteins.")

        if 'literature' in context_data and isinstance(context_data.get('literature'), dict):
            lit_data = context_data['literature'].get('selected', {})
            if isinstance(lit_data, dict):
                 count = len(lit_data)
                 total_articles = sum(len(d.get('articles', [])) for d in lit_data.values() if isinstance(d,dict))
                 summary_parts.append(f"- Stage 1 Literature: {total_articles} articles reviewed across {count} proteins.")

        if 'literature_stage2' in context_data and isinstance(context_data['literature_stage2'], dict):
             count = len(context_data['literature_stage2'])
             total_articles = sum(len(d.get('articles',[])) for d in context_data['literature_stage2'].values() if isinstance(d, dict))
             summary_parts.append(f"- Stage 2 Literature: {total_articles} targeted articles found for {count} proteins.")
        
        if 'proteins_evidence' in context_data and isinstance(context_data['proteins_evidence'], dict):
             count = len(context_data['proteins_evidence'])
             summary_parts.append(f"- Evidence summaries prepared for {count} proteins.")

        return "\n".join(summary_parts) if summary_parts else "Limited context data provided."

    def export_discussion_markdown(self, filepath: str):
        """Export discussion record as Markdown"""
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"# Expert Panel Discussion\n\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("---\n\n")

                for entry in self.discussion_history:
                    f.write(f"## {entry.get('speaker', 'Unknown')} ({entry.get('stage', 'Unknown')})\n\n")
                    f.write(f"{entry.get('content', 'No content.')}\n\n")
                    f.write("---\n\n")

            logger.info(f"Discussion exported successfully to: {filepath}")
            self.emit_log(f"Discussion exported to Markdown: {filepath}", 'success')
        except Exception as e:
            logger.error(f"Failed to export discussion to Markdown: {e}")
            self.emit_log(f"Failed to export discussion: {e}", 'error')



class ExpertSystemHelper:
    """Helper class for managing expert descriptions and prompt generation."""

    EXPERT_DESCRIPTIONS = {
        'PI (Principal Investigator)': """You are the Principal Investigator...""",
        'Molecular Toxicologist': """You are a Molecular Toxicologist...""",
        'Computational Biologist': """You are a Computational Biologist...""",
        'Cell Biologist': """You are a Cell Biologist...""",
        'Pharmacologist': """You are a Pharmacologist...""",
        'Epidemiologist': """You are an Environmental Epidemiologist...""",
        'Pathologist': """You are a Pathologist...""",
        'Developmental Toxicologist': """You are a Developmental Toxicologist...""",
        'Immunotoxicologist': """You are an Immunotoxicologist...""",
        'Biochemist': """You are a Biochemist...""",
        'Scientific Critic': """You are assigned the role of a Scientific Critic..."""
    }
    EXPERT_DESCRIPTIONS['PI (Principal Investigator)'] = """You are the Principal Investigator leading this research project focused on identifying key proteins involved in pollutant toxicity.
Your role is to:
- Define the research goals and ensure the discussion stays focused.
- Guide the conversation, asking clarifying questions and ensuring all perspectives are heard.
- Synthesize information from different experts to build a cohesive understanding.
- Identify consensus points, disagreements, and critical knowledge gaps.
- Make informed decisions on research priorities and next steps based on the evidence presented.
You should be strategic, evidence-driven, and facilitate a collaborative environment."""
    EXPERT_DESCRIPTIONS['Molecular Toxicologist'] = """You are a Molecular Toxicologist specializing in the mechanisms by which chemicals cause harm at the molecular and cellular level.
Your expertise includes:
- Xenobiotic metabolism (Phase I/II enzymes, particularly CYP450s).
- Mechanisms of cellular damage (e.g., oxidative stress, DNA damage, protein adduct formation).
- Toxicokinetics and toxicodynamics (ADME properties).
- Metabolic activation and detoxification pathways.
Focus on: How might the pollutant interacting with the protein alter its metabolic function, lead to reactive intermediates, or trigger cellular stress responses?"""
    EXPERT_DESCRIPTIONS['Computational Biologist'] = """You are a Computational Biologist specializing in bioinformatics and systems biology approaches to understand complex biological processes.
Your expertise includes:
- Analysis of large datasets (genomics, proteomics, transcriptomics).
- Biological pathway and network analysis (e.g., KEGG, Reactome).
- Protein structure and function prediction, protein-protein interactions.
- Interpretation of data from bioinformatics databases (e.g., UniProt, Gene Ontology).
Focus on: What do computational predictions, pathway databases, and interaction networks suggest about the protein's role and the potential consequences of its interaction with the pollutant?"""
    EXPERT_DESCRIPTIONS['Cell Biologist'] = """You are a Cell Biologist focused on cellular processes, signaling pathways, and responses to stress.
Your expertise includes:
- Intracellular signaling cascades (e.g., MAPK, NF-κB).
- Cellular stress responses (e.g., UPR, heat shock response).
- Mechanisms of cell death (apoptosis, necrosis, autophagy).
- Organelle function and dysfunction (mitochondria, ER).
- Cell cycle regulation.
Focus on: What specific cellular pathways or processes are likely affected if the protein's function is altered by the pollutant, and how might this lead to the observed toxic phenotype?"""
    EXPERT_DESCRIPTIONS['Pharmacologist'] = """You are a Pharmacologist with expertise in how drugs and chemicals interact with biological systems, particularly focusing on target engagement and downstream effects.
Your expertise includes:
- Drug/chemical-target interactions (receptor binding, enzyme inhibition/activation).
- Dose-response relationships and pharmacodynamics.
- Target validation and understanding on-target vs. off-target effects.
- Relating molecular interactions to physiological outcomes.
Focus on: Considering the confirmed binding, what are the likely functional consequences (inhibition, activation, altered binding)? Is the nature of the interaction consistent with the observed toxicity profile?"""
    EXPERT_DESCRIPTIONS['Epidemiologist'] = """You are an Environmental Epidemiologist studying the links between environmental exposures and human health outcomes in populations.
Your expertise includes:
- Designing and interpreting population-based studies (cohort, case-control).
- Exposure assessment methods.
- Statistical analysis of environmental health data and risk assessment.
- Evaluating human evidence for chemical toxicity.
Focus on: Is there any human population data (epidemiological studies) that supports or refutes the proposed mechanistic link between the pollutant, the protein, and the observed health effects?"""
    EXPERT_DESCRIPTIONS['Pathologist'] = """You are a Pathologist specializing in toxicologic pathology and the study of disease mechanisms at the tissue and organ level.
Your expertise includes:
- Histopathological evaluation of tissues.
- Identifying cellular and tissue-level changes associated with toxicity.
- Understanding disease progression and mechanisms.
- Correlation of molecular changes with observable pathology.
- Biomarkers of tissue damage.
Focus on: How might the proposed molecular/cellular changes manifest as observable pathology in the target tissue? Are the proposed mechanisms consistent with known pathological findings for this type of toxicity?"""
    EXPERT_DESCRIPTIONS['Developmental Toxicologist'] = """You are a Developmental Toxicologist focused on the effects of chemical exposures during development.
Your expertise includes:
- Principles of developmental biology and teratology.
- Critical windows of susceptibility during development.
- Effects on reproduction and transgenerational inheritance.
- Disruption of key developmental signaling pathways.
Focus on: Does the protein play a critical role during development? Are there potential developmental or reproductive toxicity concerns if its function is disrupted by the pollutant?"""
    EXPERT_DESCRIPTIONS['Immunotoxicologist'] = """You are an Immunotoxicologist studying the adverse effects of chemicals on the immune system.
Your expertise includes:
- Immune system function and regulation.
- Mechanisms of inflammation, autoimmunity, and immune suppression.
- Cytokine signaling and immune cell interactions.
- How xenobiotics modulate immune responses.
Focus on: Does the protein have a known role in immune function? Could modulation of this protein by the pollutant lead to immune-related effects contributing to the observed toxicity?"""
    EXPERT_DESCRIPTIONS['Biochemist'] = """You are a Biochemist specializing in protein structure, function, and enzyme kinetics.
Your expertise includes:
- Protein structure-function relationships.
- Enzyme mechanisms and kinetics (Km, Vmax, inhibition types).
- Protein binding sites and allosteric regulation.
- Post-translational modifications and their impact on function.
Focus on: How might the binding of the pollutant physically alter the protein's structure, active site, or conformational dynamics? What are the predicted biochemical consequences (e.g., changes in catalytic efficiency, substrate binding)?"""
    EXPERT_DESCRIPTIONS['Scientific Critic'] = """You are assigned the role of a Scientific Critic for this discussion. Your primary function is to rigorously challenge assumptions and ensure the analysis is based on solid evidence.
Your responsibilities include:
- Questioning interpretations, especially where evidence seems weak or ambiguous.
- Identifying potential alternative explanations for observed phenomena.
- Pointing out logical inconsistencies or gaps in the proposed mechanisms.
- Demanding clear evidence to support claims made by other experts.
- Highlighting limitations of the available data or proposed models.
Your goal is not to be obstructive, but to ensure the final conclusions are robust and well-supported. Be constructive but firm in your critique."""


    @staticmethod
    def get_expert_description(expert_name: str) -> str:
        """Get the detailed description for a given expert role."""
        role_key = expert_name.split('(')[0].strip()
        return ExpertSystemHelper.EXPERT_DESCRIPTIONS.get(
            role_key,
            f"You are {expert_name}, a scientific expert contributing to this discussion. Please provide insights based on your general scientific knowledge."
        )

    @staticmethod
    def create_round1_nomination_prompt(
        expert_name: str,
        context_data: Dict,
        valid_proteins: List[str],
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str]
    ) -> str:
        """(MODIFIED) Generate the prompt for an expert's nomination turn in Round 1."""
        expert_desc = ExpertSystemHelper.get_expert_description(expert_name)
        evidence_text = ExpertSystemHelper._format_evidence_for_prompt(
             context_data.get('proteins_evidence', {})
        )
        phenotypes_str = ', '.join(target_phenotypes)

        return f"""{expert_desc}
{LANGUAGE_CONSTRAINT}

[MEETING CONTEXT]
- Round: 1 (Expert Nominations)
- Research Question: Among proteins CONFIRMED to BIND {pollutant}, which are FUNCTIONALLY CRITICAL for {phenotypes_str} in {target_tissue}?
- Focus: Assess the FUNCTIONAL consequences of binding, NOT the binding itself.

[AVAILABLE EVIDENCE SUMMARY]
{evidence_text}
(Note: Full literature and database details were used to generate the above summaries for all {len(valid_proteins)} proteins.)

[YOUR TASK - Round 1 Nominations]
Drawing on your specific expertise ({expert_name.split('(')[0].strip()}), analyze all proteins based on the provided evidence summaries.
Your task is to **nominate the Top 3-5 most critical proteins** from the list that you believe are most likely involved in the toxic phenotype.

[Required JSON Output Format]
Return ONLY a JSON object with a single key "nominations".
{{
  "nominations": [
    {{"protein": "ProteinA", "rationale": "Your 2-3 sentence reason based on your expertise..."}},
    {{"protein": "ProteinB", "rationale": "Your reason for this nomination..."}},
    {{"protein": "ProteinC", "rationale": "Your reason for this nomination..."}}
  ]
}}

[Instructions]
- Nominate **only your top 3-5 candidates** from the full list.
- The value must be a list of JSON objects.
- Each object MUST contain 'protein' (string) and 'rationale' (string).
- **CRITICAL:** The 'rationale' field is **mandatory** for *every* nominated protein. Provide a 2-3 sentence justification for your nomination, based on your specific expert perspective (e.g., as a Biochemist, focus on metabolic pathways; as an Immunotoxicologist, focus on inflammation)."""

    @staticmethod
    def create_pi_nomination_synthesis_prompt(
        expert_nominations: Dict[str, List[Dict]]
    ) -> str:
        """(NEW) Generate prompt for PI to synthesize expert nominations."""
        
        nomination_summary_parts = ["# Expert Nominations (Collated)"]
        for protein, nominations in expert_nominations.items():
            expert_list = ", ".join([n['expert'] for n in nominations])
            rationales = "\n".join([f"    - Rationale ({n['expert']}): {n['rationale']}" for n in nominations])
            nomination_summary_parts.append(
                f"\n  Protein: **{protein}** (Nominated by: {expert_list})\n{rationales}"
            )
        
        nomination_summary_text = "\n".join(nomination_summary_parts)

        return f"""As the PI, your task is to analyze and collate the nominations from your expert team.
{LANGUAGE_CONSTRAINT}

[EXPERT NOMINATIONS (Collated)]
{nomination_summary_text}

[YOUR TASK - Synthesize Nominations]
Review the collated nominations. Your task is to group these proteins and present a summary.
- Identify "High Consensus" proteins (nominated by 2 or more experts).
- Identify "Specialist Interest" proteins (nominated by only 1 expert).

[Required JSON Output Format]
Return ONLY a JSON object with two keys: "high_consensus" and "specialist_interest".

{{
  "high_consensus": [
    {{"protein": "ProteinX", "nominated_by": ["ExpertA", "ExpertB"], "rationales": ["Rationale from ExpertA...", "Rationale from ExpertB..."]}},
    ...
  ],
  "specialist_interest": [
    {{"protein": "ProteinZ", "nominated_by": "ExpertC", "rationale": "Rationale from ExpertC..."}},
    ...
  ]
}}

[CRITICAL INSTRUCTIONS]
1.  Accurately collate the "high_consensus" group, listing all nominating experts and their rationales.
2.  Accurately collate the "specialist_interest" group, listing the single expert and their rationale.
3.  The lists MUST NOT be empty if nominations were provided.
"""

    @staticmethod
    def create_critic_questions_prompt(
        pi_synthesis_json: Dict
    ) -> str:
        """(MODIFIED) Generate prompt for Critic to ask questions based on nominations."""
        
        try:
            synthesis_text_parts = ["# PI's Nomination Summary"]
            high_consensus = pi_synthesis_json.get('high_consensus', [])
            specialist_interest = pi_synthesis_json.get('specialist_interest', [])
            
            synthesis_text_parts.append("\n**High Consensus Proteins:**")
            if high_consensus:
                for item in high_consensus:
                    synthesis_text_parts.append(f"- {item.get('protein')} (by {', '.join(item.get('nominated_by', []))})")
            else:
                synthesis_text_parts.append("- None")
                
            synthesis_text_parts.append("\n**Specialist Interest Proteins:**")
            if specialist_interest:
                for item in specialist_interest:
                    synthesis_text_parts.append(f"- {item.get('protein')} (by {item.get('nominated_by')})")
            else:
                synthesis_text_parts.append("- None")
            
            synthesis_summary_text = "\n".join(synthesis_text_parts)
        except Exception as e:
            logger.error(f"Error formatting critic prompt summary: {e}")
            synthesis_summary_text = f"Error summarizing nominations: {pi_synthesis_json}"

        
        return f"""As the Scientific Critic, your task is to challenge the preliminary consensus derived from expert nominations.
{LANGUAGE_CONSTRAINT}

[PI'S NOMINATION SUMMARY]
{synthesis_summary_text}

[YOUR TASK - Identify Key Questions]
Strictly review this nomination-based summary. Identify 3-5 critical questions, potential conflicts, or evidence gaps.
Focus on:
-   **Conflicts:** Why did one expert (e.g., Biochemist) nominate a protein (e.g., G6pc1) that another (e.g., Immunotoxicologist) completely ignored?
-   **Weak Rationale:** Are the 'High Consensus' proteins supported by strong, converging rationales, or just superficial agreement?
-   **Evidence Gaps:** What critical data (e.g., tissue expression, literature links) are ALL experts *assuming* but was NOT explicitly provided in the evidence summary?
-   **Overlooked Proteins:** Is there a protein that *wasn't* nominated but whose evidence summary suggests it should be a candidate?

[Required JSON Output Format]
Return ONLY a JSON object with a single key "critic_questions". The value must be a list of strings.
{{
  "critic_questions": [
    "Question 1: The Biochemist nominated G6pc1 as critical for metabolism, but the Immunotoxicologist ignored it. We need to resolve if its metabolic role or lack of immune role is more important.",
    "Question 2: All experts are nominating proteins like Mpo and Lta4h based on inflammation, but the evidence summary showed '0 articles' and 'No expression data' for both. We are operating on pure assumption.",
    "Question 3: ..."
  ]
}}"""

    @staticmethod
    def create_round2_expert_prompt(
        expert_name: str,
        context_data: Dict,
        recent_discussion: str,
        round_num: int,
        total_rounds: int,
        proteins_summary: List[str],
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str]
    ) -> str:
        """(MODIFIED) Generate the prompt for an expert's turn in Round 2 (Final Evaluation)."""
        expert_desc = ExpertSystemHelper.get_expert_description(expert_name)
        
        round1_ranking = context_data.get('round1_ranking', []) 
        critic_questions = context_data.get('critic_questions', [])
        
        summary_preview = proteins_summary[0][:600] + "..." if proteins_summary else "No detailed summary available."
        phenotypes_str = ', '.join(target_phenotypes)

        return f"""{expert_desc}
{LANGUAGE_CONSTRAINT}

[MEETING CONTEXT]
- Round: {round_num} of {total_rounds} - FINAL EVALUATION
- Research Question: Based on ALL available evidence (Stage 1 + Stage 2 targeted literature), how does {pollutant} binding to each protein functionally contribute to {phenotypes_str} in {target_tissue}?

[ROUND 1 PRELIMINARY RANKING (based on Nominations)]
{', '.join(round1_ranking)}

[CRITIC'S KEY QUESTIONS FROM R1]
- {chr(10).join(f"- {q}" for q in critic_questions)}

[EVIDENCE FOR REVIEW]
Comprehensive evidence summaries, including Stage 1 and Stage 2 targeted literature findings (which addressed the Critic's questions), are now available.
Example Evidence Snippet:
{summary_preview}
(Full details inform this round.)

[RECENT DISCUSSION HIGHLIGHTS]
{recent_discussion}

[YOUR TASK - Final Discussion & Re-evaluation]
Focusing on proteins relevant to your expertise ({expert_name.split('(')[0].strip()}), provide your final assessment:
1.  **Address Critic's Questions:** How does the new Stage 2 literature (summarized in the evidence) answer the Critic's questions?
2.  **Revise Ranking:** Do you agree or disagree with the Round 1 preliminary ranking? Propose any specific modifications based on the new evidence and discussion.
3.  **Confidence Assessment:** State your final confidence (High/Medium/Low) in the top-ranked proteins.

[Response Guidelines]
- Directly address critiques or gaps identified in Round 1 using the new Stage 2 evidence.
- Justify any proposed changes to the ranking.
- You may say "pass" if you have no further comments.
- Aim for 3-5 paragraphs per protein assessed."""

    @staticmethod
    def create_pi_final_ranking_and_plan_prompt(pi_name: str) -> str:
        """(MODIFIED) Prompt for _pi_final_summary to generate JSON output."""
        
        return f"""As the {pi_name}, synthesize the entire discussion into a final JSON object.
{LANGUAGE_CONSTRAINT}

[TASK]
Based on the entire discussion, determine the final consensus.
1.  Identify the **final list of critical proteins** (final_ranking).
    **CRITICAL CONSTRAINT: You must ONLY select proteins from the original Candidate Protein List provided in the context. DO NOT invent new proteins or include intermediates like Foxo1 unless they were in the original input.**
2.  Formulate a specific **experimental validation plan** for *each* protein in that final list (research_plans).
3.  Write a brief **summary text** (final_summary_text) describing the mechanistic hypothesis and key findings.

[Required JSON Output Format]
Return ONLY a JSON object. Do not include any text outside the JSON.

{{
  "final_ranking": [
    "ProteinA", 
    "ProteinB", 
    "ProteinC"
  ],
  "research_plans": [
    {{
      "protein": "ProteinA", 
      "plan": "Specific, actionable experimental plan to validate ProteinA's role..."
    }},
    {{
      "protein": "ProteinB", 
      "plan": "Specific, actionable experimental plan to validate ProteinB's role..."
    }},
    {{
      "protein": "ProteinC", 
      "plan": "Specific, actionable experimental plan to validate ProteinC's role..."
    }}
  ],
  "final_summary_text": "A brief text summary of the final hypothesis, key findings, and remaining uncertainties for logging purposes."
}}
"""

    @staticmethod
    def _format_evidence_summary(proteins_evidence: Dict[str, Dict]) -> str:
        """Format basic evidence summary for prompts (Original simple version)."""
        if not proteins_evidence:
            return "No evidence summary available."

        summaries = []
        for protein, evidence in proteins_evidence.items():
            if not isinstance(evidence, dict): continue

            summary = f"\n[{protein}]\n"
            evidence_summary_data = evidence.get('evidence_summary', {})
            db_features = evidence_summary_data.get('functional_evidence', {}).get('database_features', {})
            if db_features:
                 functions = db_features.get('molecular_functions', [])
                 pathways = db_features.get('pathways', [])
                 summary += f"  Database: {len(functions)} functions, {len(pathways)} pathways linked.\n"
            else:
                 summary += "  Database: Limited functional data.\n"

            lit_summary = evidence_summary_data.get('literature_evidence', {})
            if lit_summary:
                total = lit_summary.get('total_articles', 0)
                func_count = lit_summary.get('function_articles_count', 0)
                int_count = lit_summary.get('interaction_articles_count', 0)
                avg_rel = lit_summary.get('average_relevance', 0)
                summary += f"  Literature: {total} articles found (Func: {func_count}, Interact: {int_count}). Avg Relevance: {avg_rel:.1f}/10.\n"
            else:
                 summary += "  Literature: No articles summarized.\n"

            summaries.append(summary)

        return '\n'.join(summaries) if summaries else "No protein evidence could be summarized."

    @staticmethod
    def _format_evidence_for_prompt(proteins_evidence_summaries: Dict[str, str]) -> str:
        """Formats the pre-generated evidence summaries for inclusion in prompts."""
        if not proteins_evidence_summaries:
            return "No evidence summaries available for the proteins under discussion."

        formatted_list = []
        for protein, summary_text in proteins_evidence_summaries.items():
             formatted_list.append(f"--- Evidence Summary for {protein} ---\n{summary_text}\n")

        return "\n".join(formatted_list)


    @staticmethod
    def create_protein_evidence_summary(protein: str, fp_analysis: Dict) -> str:
        """(MODIFIED V2) Create a concise evidence summary string for a single protein from FP analysis."""
        if not fp_analysis or not isinstance(fp_analysis, dict):
            return f"#### {protein}\n- **Known Function:** No analysis data available.\n- **Tissue Expression:** No analysis data available.\n- **Literature Hint:** No analysis data available.\n"

        summary = f"#### {protein}\n\n"
        evidence_sum = fp_analysis.get('evidence_summary', {})
        func_ev = evidence_sum.get('functional_evidence', {})
        
        db_features = func_ev.get('database_features', {})
        functions = db_features.get('molecular_functions', [])
        pathways = db_features.get('pathways', [])
        
        raw_db_data = fp_analysis.get('functional_data', {}).get('raw_data', {})
        kegg_details = raw_db_data.get('kegg', {}).get('details', {})
        uniprot_details = raw_db_data.get('uniprot', {})

        kegg_diseases = kegg_details.get('disease_associations', [])
        uniprot_keywords = uniprot_details.get('keywords', [])

        if functions or pathways or kegg_diseases or uniprot_keywords:
            summary += "- **Known Function (KEGG/UniProt):**\n"
            if pathways:
                top_pathways = [p.get('pathway_name', 'N/A') for p in pathways[:5] if isinstance(p, dict)]
                summary += f"    - **KEGG Pathways ({len(pathways)}):** {'; '.join(top_pathways)}"
                if len(pathways) > 5: summary += ", ...\n"
                else: summary += "\n"
            else:
                 summary += "    - **KEGG Pathways:** Not found\n"

            if kegg_diseases:
                top_diseases = [d.get('disease_name', 'N/A').split(' [')[0] for d in kegg_diseases[:3] if isinstance(d, dict)]
                summary += f"    - **KEGG Diseases ({len(kegg_diseases)}):** {'; '.join(top_diseases)}"
                if len(kegg_diseases) > 3: summary += ", ...\n"
                else: summary += "\n"

            if functions:
                top_functions = functions[:3]
                summary += f"    - **UniProt Functions ({len(functions)}):** {'; '.join(top_functions)}"
                if len(functions) > 3: summary += ", ...\n"
                else: summary += "\n"
            else:
                summary += "    - **UniProt Functions:** Not found\n"

            if uniprot_keywords:
                top_keywords = uniprot_keywords[:5]
                summary += f"    - **UniProt Keywords ({len(uniprot_keywords)}):** {'; '.join(top_keywords)}"
                if len(uniprot_keywords) > 5: summary += ", ...\n"
                else: summary += "\n"
        else:
            summary += "- **Known Function (KEGG/UniProt):** No functional or pathway entries found in databases.\n"

        expr_ev = evidence_sum.get('expression_evidence', {})
        if expr_ev and expr_ev.get('source'):
            summary += (
                f"- **Tissue Expression ({expr_ev.get('tissue', 'N/A')}, {expr_ev.get('source', 'N/A')}):** "
                f"{expr_ev.get('level', 'N/A')} ({expr_ev.get('tpm', 0)} TPM, {expr_ev.get('percentile', 0)}% percentile)\n"
            )
        else:
            summary += "- **Tissue Expression:** No HPA/GTEx data found for human ortholog.\n"

        lit_ev = evidence_sum.get('literature_evidence', {})
        int_articles_count = lit_ev.get('interaction_articles_count', 0) 
        summary += f"- **Literature Hint:** {int_articles_count} articles found potentially linking protein to pollutant/phenotype.\n"

        return summary


    @staticmethod
    def format_round1_agenda(
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str],
        valid_proteins: List[str]
    ) -> str:
        """(MODIFIED) Format the agenda specifically for Round 1."""
        phenotypes_str = ', '.join(target_phenotypes)

        return f"""
[MEETING AGENDA - ROUND 1: NOMINATION & CRITIQUE]

Goal: Conduct an expert-driven nomination of key proteins to focus subsequent analysis.

Research Question:
Among proteins CONFIRMED to BIND {pollutant}, which are most likely FUNCTIONALLY CRITICAL for {phenotypes_str} in {target_tissue}?

Candidate Proteins for Evaluation:
{len(valid_proteins)} proteins (e.g., {', '.join(valid_proteins[:3])}...)

Phase 1 Tasks (Experts):
1.  Review all protein evidence summaries.
2.  **Nominate your Top 3-5 most critical proteins** with a 2-3 sentence rationale for each.

Phase 2 Task (PI):
1.  Synthesize all expert nominations into "High Consensus" and "Specialist Interest" groups.

Phase 3 Task (Critic):
1.  Rigorously review the nominated protein groups and rationales.
2.  Identify 3-5 critical questions, conflicts, and evidence gaps to be addressed in Round 2.
"""

    @staticmethod
    def format_round2_agenda(
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str],
        proteins_summary: List[str],
        round1_ranking: List[str],
        critic_questions: List[str]
    ) -> str:
        """(MODIFIED) Format the agenda specifically for Round 2 (Final Evaluation)."""
        phenotypes_str = ', '.join(target_phenotypes)

        return f"""
[MEETING AGENDA - ROUND 2: FINAL EVALUATION & PLANNING]

Goal: Refine the preliminary ranking based on new evidence and formulate actionable research plans.

Central Question Recap:
"Based on all data, what is the final ranking, and what are the next steps?"

Inputs for this Round:
1.  R1 Preliminary Ranking: {', '.join(round1_ranking[:5])}...
2.  R1 Critic's Questions: {len(critic_questions)} questions (e.g., "{critic_questions[0] if critic_questions else '...'}?")
3.  Stage 2 Literature: New targeted literature has been retrieved to address these questions.

Tasks for All Experts:
1.  Discuss the Critic's questions using the new Stage 2 literature.
2.  Debate and propose revisions to the Round 1 ranking.
3.  Propose specific validation experiments for top candidates.

Final Task (PI):
1.  Synthesize the discussion into a list of "Final Identified Proteins".
2.  Summarize the "Mechanistic Hypothesis" for these proteins.
3.  Formulate a "Experimental Validation Plan" for *each* identified protein.
"""
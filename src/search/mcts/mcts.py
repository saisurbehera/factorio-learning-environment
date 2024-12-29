import asyncio
import json
import re
from asyncio import sleep
from typing import Optional, List

import psycopg2
import tenacity
from tenacity import wait_exponential, retry, retry_if_exception_type

from search.model.conversation import Conversation, Message, GenerationParameters
from search.mcts.conversation_formatter import ConversationFormatter, DefaultFormatter
from search.mcts.db_client import DBClient
from search.mcts.factorio_evaluator import FactorioEvaluator
from search.model.game_state import GameState
from search.model.program import Program
from search.mcts.samplers.db_sampler import DBSampler


class MCTS:
    def __init__(self,
                 llm_factory: 'LLMFactory',
                 db_client: DBClient,
                 evaluator: FactorioEvaluator,
                 sampler: DBSampler,
                 system_prompt: str,
                 initial_state: GameState,
                 formatter: ConversationFormatter = DefaultFormatter(),
                 version=1,
                 version_description="",
                 presence_penalty=0,
                 frequency_penalty=0,
                 error_penalty=0
                 ):

        self.llm = llm_factory
        self.db = db_client
        self.evaluator = evaluator
        self.system_prompt = system_prompt
        self.initial_state = initial_state
        self.sampler = sampler
        self.version = version
        self.version_description=version_description
        self.formatter = formatter
        self.retry_count = 0
        self.max_retries = 3
        self.presence_penalty = presence_penalty
        self.frequency_penalty = frequency_penalty
        self.error_penalty = error_penalty


    def _verify_response_is_python(self, content):
        code = content
        # Parse into an AST to verify that this is a program
        try:
            ast = compile(code, filename="<ast>", mode="exec")
        except SyntaxError:
            # Take the last line off and try again
            code = code.rsplit('\n', 1)[0] + '\n'
            ast = compile(code, filename="<ast>", mode="exec")

        return code

    def _extract_code_from_choice(self, choice) -> Optional[str]:
        """Extract code from a single completion choice"""
        if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
            content = choice.message.content
        elif hasattr(choice, 'text'):
            content = choice.text
        else:
            raise RuntimeError('Incorrect message format')

        try:
            code = self._verify_response_is_python(content)
            return code, content
        except Exception as e:
            try:
                # Get all text between triple backticks with regex: ```\n?python(.+\n?)```
                code = re.findall(r'(?s)```(python)?\s*(.*?)\s*```', content)[0][1]

                #code = content.replace("```python", "").replace('```', '')
                code = self._verify_response_is_python(code)
                return code, content
            except Exception as e1:
                # Sometimes it samples a leading line, before writing unblocked python code.
                content = "\n".join(content.split("\n")[1:])
                try:
                    code = self._verify_response_is_python(content)
                    return code, content
                except Exception as e2:
                    try:
                        content_split = content.split('from factorio_instance import *')
                        code = content_split[1].strip()
                        text_response = content_split[0].strip()
                        code = self._verify_response_is_python(code)
                        return code, text_response
                    except Exception as e2:
                        #print(f"Failed to extract code from choice after removing leading line and factorio_instance import: {str(e2)} \n\n`{content}`")
                        chain_of_thoughts = '"""\n'+content.strip().strip("\"")+'\n"""'
                        return chain_of_thoughts, content.strip()
                    #print(f"Failed to extract code from choice after removing leading line: {str(e2)}")
                print(f"Failed to extract code from choice: {str(e1)}")

    @retry(wait=wait_exponential(multiplier=1, min=4, max=10))
    async def _generate_programs_batch(self, conversation: Conversation,
                                       generation_params: GenerationParameters,
                                       meta={}
                                       ) -> List[Program]:
        """Generate multiple programs either through OpenAI's n parameter or parallel calls"""
        formatted_messages = self.formatter.to_llm_messages(
            self.formatter.format_conversation(conversation)
        )

        try:
            messages = conversation.model_dump()['messages']
        except Exception:
            messages = conversation.dict()['messages']

        try:
            if "gpt" in generation_params.model or 'o1' in generation_params.model and hasattr(self.llm, "acall"):
                # Use OpenAI's native n parameter support
                response = await self.llm.acall(
                    messages=formatted_messages,
                    n_samples=generation_params.n,
                    temperature=generation_params.temperature,
                    max_tokens=generation_params.max_tokens,
                    logit_bias=generation_params.logit_bias,
                    stop_sequences=generation_params.stop_sequences,
                    model=generation_params.model,
                    presence_penalty=self.presence_penalty,
                    frequency_penalty=self.frequency_penalty
                )
                return await self._process_openai_response(response, conversation, generation_params, messages, meta)
            else:
                # Make parallel calls for other providers
                return await self._generate_parallel(
                    conversation,
                    generation_params,
                    formatted_messages,
                    messages,
                    meta
                )
        except Exception as e:
            print(f"Batch program generation failed: {str(e)}")
            return []

    async def _generate_parallel(self, conversation, generation_params, formatted_messages,
                                 messages, meta) -> List[Program]:
        """Generate n programs in parallel for providers that don't support batch generation"""

        async def single_generation():
            try:
                response = await self.llm.acall(
                    messages=formatted_messages,
                    n_samples=1,  # Force single sample
                    temperature=generation_params.temperature,
                    max_tokens=generation_params.max_tokens,
                    logit_bias=generation_params.logit_bias,
                    stop_sequences=generation_params.stop_sequences,
                    model=generation_params.model,
                    presence_penalty=self.presence_penalty,
                    frequency_penalty=self.frequency_penalty
                )
                await sleep(2) # Sleep to avoid rate limiting issues
                return response
            except Exception as e:
                print(f"Single generation failed: {str(e)}")
                return None

        # Generate n responses in parallel
        responses = await asyncio.gather(
            *[single_generation() for _ in range(generation_params.n)],
            return_exceptions=True
        )

        # Process successful responses
        programs = []
        for response in responses:
            if response is not None and not isinstance(response, Exception):
                program = await self._create_program(
                    response, conversation, messages,
                    generation_params.model, meta
                )
                if program:
                    programs.append(program)

        return programs

    async def _create_program(self, response, conversation, messages, model, meta) -> Program:
        """Create a Program instance from a single response"""
        if 'choices' in response:
            choice = response.choices[0]  # Assuming only one choice per call
            input_tokens = response.usage.prompt_tokens if hasattr(response, 'usage') else 0
            output_tokens = response.usage.completion_tokens if hasattr(response, 'usage') else 0
            total_tokens = input_tokens + output_tokens
        else:
            choice = response.content[0]
            input_tokens = response.usage.input_tokens if hasattr(response, 'usage') else 0
            output_tokens = response.usage.output_tokens if hasattr(response, 'usage') else 0
            total_tokens = input_tokens + output_tokens

        code, text_response = self._extract_code_from_choice(choice)
        if not code:
            return None

        program = Program(
            id=hash((code, json.dumps(messages))),
            code=code,
            conversation=conversation,
            response=code,
            token_usage=total_tokens,
            completion_token_usage=output_tokens,
            prompt_token_usage=input_tokens,
            version=self.version,
            version_description=self.version_description,
            meta={"text_response": text_response, "model": model}
        )

        if meta:
            program.meta.update(meta)

        return program

    async def _process_openai_response(self, response, conversation, generation_params,
                                       messages, meta) -> List[Program]:
        """Process OpenAI's response with multiple choices"""
        programs = []
        for choice in response.choices:
            code, text_response = self._extract_code_from_choice(choice)
            if code:
                programs.append(Program(
                    id=hash((code, json.dumps(messages))),
                    code=code,
                    conversation=conversation,
                    response=choice.message.content,
                    token_usage=response.usage.total_tokens // generation_params.n if hasattr(response,
                                                                                              'usage') else None,
                    completion_token_usage=response.usage.completion_tokens // generation_params.n if hasattr(response,
                                                                                                              'usage') else None,
                    prompt_token_usage=response.usage.prompt_tokens // generation_params.n if hasattr(response,
                                                                                                      'usage') else None,
                    version=self.version,
                    version_description=self.version_description,
                    meta={"text_response": text_response, "model": generation_params.model}
                ))
                if meta:
                    programs[-1].meta.update(meta)
        return programs

    async def search(self,
                     n_iterations: int,
                     samples_per_iteration: int,
                     skip_failures: bool = False):
        """
        Search for the best program using Monte Carlo Tree Search (MCTS).
        :param n_iterations: Number of iterations to perform.
        :param samples_per_iteration: Number of programs to sample per iteration.
        :param skip_failures: Whether to skip saving failed program generations.
        """

        for iteration in range(n_iterations):
            print(f"Starting iteration {iteration}")
            await self.run_iteration(samples_per_iteration, skip_failures, iteration, n_iterations)
            self.evaluator.logger.update_progress()

    @tenacity.retry(
        retry=retry_if_exception_type(psycopg2.Error),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        stop=tenacity.stop_after_attempt(3)
    )
    async def run_iteration(self, samples_per_iteration, skip_failures, iteration, n_iterations):
        """Run a single MCTS iteration with retries for concurrent operations"""
        try:
            parent = await self.sampler.sample_parent(version=self.version)
            if parent:
                start_state = parent.state
                conversation = parent.conversation
            else:
                start_state = self.initial_state
                self.evaluator.instances[0].reset(start_state)
                entities = self.evaluator.instances[0].get_entities()
                conversation = Conversation(messages=[
                    Message(role="system", content=self.system_prompt),
                    # Message(role="user", content=PLANNING_ADDITION_PROMPT),
                    Message(role="assistant", content="print(f'Inventory: {inspect_inventory()}')\n"
                                                      "print(f'Entities: {get_entities()}')\n"),
                    Message(role="user", content=f"1: ('Inventory: {start_state.inventory.__dict__}')\n"
                                                 f"2: ('Entities: {entities}')"),
                ])

            self.evaluator.set_sampling_status()
            self.evaluator.set_iteration(iteration, n_iterations)
            generation_parameters = GenerationParameters(n = samples_per_iteration,
                                                         model = self.llm.model,
                                                         presence_penalty=0.7)
            # Generate multiple programs from same parent
            programs = await self._generate_programs_batch(conversation, generation_parameters)
            if not programs:
                return

            programs = [p for p in programs if p is not None]
            for program in programs:
                program.parent_id = parent.id if parent else None

            evaluated_programs = await self.evaluator.evaluate_batch(programs, start_state)

            # Use a connection pool or new connections for parallel saves
            save_tasks = []
            for program in evaluated_programs:
                if program.state is not None:
                    if not skip_failures or program.value is not None:
                        save_tasks.append(self.db.create_program(program))

            if save_tasks:
                await asyncio.gather(*save_tasks)

        except Exception as e:
            self.retry_count += 1
            if self.retry_count >= self.max_retries:
                print(f"Max retries ({self.max_retries}) reached. Error: {str(e)}")
                self.retry_count = 0
                raise e
            raise e

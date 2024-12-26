from distutils.command.config import config
from typing import Optional, Dict, Union, Tuple
from dataclasses import dataclass, field
from enum import Enum
import argparse
from pathlib import Path
import questionary
from search.mcts.model.game_state import GameState
from llm_factory import LLMFactory


class MCTSType(Enum):
    CHUNKED = "chunked"
    PLANNING = "planning"
    NORMAL = "mcts"
    OBJECTIVE = "objective"

class SamplerType(Enum):
    KLD = "kld"
    WEIGHTED_REWARD = "weighted reward"

@dataclass
class SamplerConfig:
    temperature: Optional[float] = 0.7
    compression_strength: Optional[float] = None
    max_conversation_length: Optional[int] = 30
    adaptive_period: Optional[int] = 200
    window_size: Optional[int] = 200


@dataclass
class BaseConfig:
    mcts_type: MCTSType
    system_prompt: str
    version: int
    version_description: Optional[str] = ""
    n_parallel: int = 4
    max_steps: int = 1000
    skip_failures: bool = False
    model: Optional[str] = None
    sampler_type: Optional[SamplerType] = None
    presence_penalty: float = 0
    frequency_penalty: float = 0


@dataclass
class PlanningConfig(BaseConfig):
    planning_model: str = "claude-3-5-sonnet-20241022"
    executor_model: str = "ft:gpt-4o-2024-08-06:paperplane-ai:fact-instruct-1:ATSVGf4d:ckpt-step-214"
    objective_model: str = "ft:gpt-4o-2024-08-06:paperplane-ai:fact-self-gen-planning:AQzcPI91"
    step_executor_prompt_path: Path = Path("../../prompts/bottoms_up_prompts/finetuning_prompts/step_supervised")
    step_generator_prompt_path: Path = Path("../../prompts/bottoms_up_prompts/finetuning_prompts/step_generator")
    step_judge_prompt_path: Path = Path("../../prompts/bottoms_up_prompts/finetuning_prompts/step_judge")
    example_plan_prompt_path: Path = Path("../../prompts/bottoms_up_prompts/finetuning_prompts/executor_plan")
    max_steps_per_objective: int = 8
    number_of_steps_for_judge: int = 3
    n_parallel: int = 8


@dataclass
class ChunkedConfig(BaseConfig):
    max_conversation_length: int = 50
    logit_bias: Dict[str, float] = field(default_factory=lambda: {
        "15714": -100,  # 'LINE'
        '193595': -100, # 'LINES'
        "145968": -100,  # ' CUT'
        "27": -100,  # '<'
        "20225": -100,  # '/>'
        "7032": -100  # 'while'
    })

@dataclass
class ObjectiveConfig(ChunkedConfig):
    objective_model: str = "ft:gpt-4o-mini-2024-07-18:paperplane-ai:plans-tree:AcZ8gHSo"



def _get_sampler(sampler_type: SamplerType,
                 db_client,
                 compression_strength = None,
                 max_conversation_length=20,
                 adaptive_period=200,
                 window_size: int = 300,
                 temperature: float = 1.0):
    from search.mcts.samplers.kld_achievement_sampler import KLDiversityAchievementSampler
    from search.mcts.samplers.dynamic_reward_weighted_sampler import DynamicRewardWeightedSampler

    if sampler_type == SamplerType.KLD:
        return KLDiversityAchievementSampler(db_client, window_size, temperature)
    return DynamicRewardWeightedSampler(db_client, compression_strength, max_conversation_length, adaptive_period)

class MCTSFactory:
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not MCTSFactory._initialized:
            self.db_client = None
            self.llm_factory = None
            self.instances = None
            self.sampler = None
            MCTSFactory._initialized = True


    def initialize(self, instances, db_client, config: Union[BaseConfig, PlanningConfig, ChunkedConfig], sampler_config: SamplerConfig):
        self.instances = instances
        self.db_client = db_client
        self.config = config
        self.llm_factory = LLMFactory(model=config.model)
        self.sampler = _get_sampler(config.sampler_type, db_client, **sampler_config.__dict__)

    def create_mcts(self, config: Union[BaseConfig, PlanningConfig, ChunkedConfig, ObjectiveConfig]):
        if not all([self.instances, self.db_client, self.llm_factory, self.sampler]):
            raise ValueError("Factory not initialized. Call initialize() first.")

        if config.mcts_type == MCTSType.CHUNKED:
            return self._create_chunked_mcts(config)
        elif config.mcts_type == MCTSType.PLANNING:
            return self._create_planning_mcts(config)
        elif config.mcts_type == MCTSType.OBJECTIVE:
            return self._create_objective_mcts(config)
        elif config.mcts_type == MCTSType.NORMAL:
            return self._create_mcts(config)

        raise ValueError(f"Unknown MCTS type: {config.mcts_type}")

    def _create_mcts(self, config: BaseConfig):
        from search.mcts.mcts import MCTS
        from search.mcts.parallel_mcts import ParallelMCTS
        from search.mcts.parallel_mcts_config import ParallelMCTSConfig

        mcts_config = ParallelMCTSConfig(
            n_parallel=config.n_parallel,
            system_prompt=config.system_prompt,
            initial_state=GameState.from_instance(self.instances[0]),
            mcts_class=MCTS,
            sampler=self.sampler,
            mcts_kwargs={
                'version': config.version,
                'version_description': config.version_description,
            }
        )
        return ParallelMCTS(
            instances=self.instances,
            db_client=self.db_client,
            llm_factory=self.llm_factory,
            config=mcts_config,
            version=config.version,
            version_description=config.version_description
        )

    def _create_chunked_mcts(self, config: ChunkedConfig):
        from search.mcts.chunked_mcts import ChunkedMCTS
        from search.mcts.parallel_mcts import ParallelMCTS
        from search.mcts.parallel_mcts_config import ParallelMCTSConfig
        from search.mcts.conversation_formatter import StructurePreservingFormatter

        mcts_config = ParallelMCTSConfig(
            n_parallel=config.n_parallel,
            system_prompt=config.system_prompt,
            initial_state=GameState.from_instance(self.instances[0]),
            mcts_class=ChunkedMCTS,
            sampler=self.sampler,
            mcts_kwargs={
                'logit_bias': config.logit_bias,
                'version': config.version,
                'version_description': config.version_description,
                'formatter': StructurePreservingFormatter(planning=True),
                'presence_penalty': config.presence_penalty,
                'frequency_penalty': config.frequency_penalty,
            }
        )

        return ParallelMCTS(
            instances=self.instances,
            db_client=self.db_client,
            llm_factory=self.llm_factory,
            config=mcts_config,
            version=config.version,
            version_description=config.version_description
        )

    def _create_objective_mcts(self, config: ObjectiveConfig):
        from search.mcts.objective_mcts import ObjectiveMCTS
        from search.mcts.parallel_mcts import ParallelMCTS
        from search.mcts.parallel_mcts_config import ParallelMCTSConfig
        from search.mcts.conversation_formatter import StructurePreservingFormatter

        mcts_config = ParallelMCTSConfig(
            n_parallel=config.n_parallel,
            system_prompt=config.system_prompt,
            initial_state=GameState.from_instance(self.instances[0]),
            mcts_class=ObjectiveMCTS,
            sampler=self.sampler,
            mcts_kwargs={
                'objective_model': config.objective_model,
                'logit_bias': config.logit_bias,
                'version': config.version,
                'version_description': config.version_description,
                'formatter': StructurePreservingFormatter(planning=True),
                'presence_penalty': config.presence_penalty,
                'frequency_penalty': config.frequency_penalty,
            }
        )

        return ParallelMCTS(
            instances=self.instances,
            db_client=self.db_client,
            llm_factory=self.llm_factory,
            config=mcts_config,
            version=config.version,
            version_description=config.version_description
        )

    def _create_planning_mcts(self, config: PlanningConfig):
        from search.mcts.planning_mcts import PlanningMCTS
        from search.mcts.parallel_planning_mcts import ParallelPlanningMCTS
        from search.mcts.parallel_mcts_config import ParallelMCTSConfig

        game_state = GameState.from_instance(self.instances[0])
        mcts_config = ParallelMCTSConfig(
            n_parallel=config.n_parallel,
            mcts_class=PlanningMCTS,
            sampler=self.sampler,
            system_prompt=config.system_prompt,
            initial_state=game_state,
            max_steps_per_objective=config.max_steps_per_objective,
            number_of_steps_for_judge=config.number_of_steps_for_judge,
            mcts_kwargs={
                "planning_model": config.planning_model,
                "executor_model": config.executor_model,
                "objective_model": config.objective_model,
                "step_executor_prompt_path": config.step_executor_prompt_path,
                "step_generator_prompt_path": config.step_generator_prompt_path,
                "step_judge_prompt_path": config.step_judge_prompt_path,
                "example_plan_prompt_path": config.example_plan_prompt_path,
                "system_prompt": config.system_prompt,
                "initial_state": game_state
            }
        )

        return ParallelPlanningMCTS(
            instances=self.instances,
            db_client=self.db_client,
            llm_factory=self.llm_factory,
            config=mcts_config,
            version=config.version,
            version_description=config.version_description
        )

    @staticmethod
    def get_config_from_cli(default_version=42) -> Tuple[Union[BaseConfig, PlanningConfig, ChunkedConfig], SamplerConfig]:
        parser = argparse.ArgumentParser()
        parser.add_argument('--type', choices=['chunked', 'planning', 'normal', 'objective'], help='MCTS type')
        parser.add_argument('--no-interactive', action='store_true', help='Skip interactive prompts')
        args, _ = parser.parse_known_args()

        if args.no_interactive:
            return MCTSFactory._get_config_from_args(parser)
        return MCTSFactory._get_config_interactive(args.type, default_version)

    @staticmethod
    def _get_config_from_args(parser) -> Tuple[Union[BaseConfig, PlanningConfig, ChunkedConfig], SamplerConfig]:
        parser.add_argument('--model', required=True)
        parser.add_argument('--version', type=int, required=True)
        parser.add_argument('--version-description', required=True)
        parser.add_argument('--n-parallel', type=int, default=4)

        args = parser.parse_args()
        mcts_type = MCTSType(args.type)

        if mcts_type == MCTSType.PLANNING:
            mcts_config = PlanningConfig(
                mcts_type=mcts_type,
                model=args.model,
                version=args.version,
                version_description=args.version_description,
                n_parallel=args.n_parallel,
                system_prompt='',
                planning_model=args.planning_model,
                executor_model=args.executor_model,
                objective_model=args.objective_model,
                step_executor_prompt_path=Path(args.step_executor_prompt_path),
                step_generator_prompt_path=Path(args.step_generator_prompt_path),
                step_judge_prompt_path=Path(args.step_judge_prompt_path),
                example_plan_prompt_path=Path(args.example_plan_prompt_path)
            )
        elif mcts_type == MCTSType.CHUNKED:
            mcts_config = ChunkedConfig(
                mcts_type=mcts_type,
                model=args.model,
                version=args.version,
                version_description=args.version_description,
                n_parallel=args.n_parallel,
                system_prompt=''
            )
        elif mcts_type == MCTSType.OBJECTIVE:
            mcts_config = ObjectiveConfig(
                objective_model=args.objective_model,
                mcts_type=mcts_type,
                model=args.model,
                version=args.version,
                version_description=args.version_description,
                n_parallel=args.n_parallel,
                system_prompt=''
            )
        else:
            mcts_config = BaseConfig(
                mcts_type=mcts_type,
                model=args.model,
                version=args.version,
                version_description=args.version_description,
                n_parallel=args.n_parallel,
                system_prompt=''
            )

        return mcts_config, SamplerConfig(temperature=args.temperature,
                                          compression_strength = args.compression_strength,
                                          max_conversation_length=args.max_conversation_length,
                                          adaptive_period=args.adaptive_period,
                                          window_size=args.window_size)


    @staticmethod
    def _get_config_interactive(default_type=None, default_version=42) -> \
            Tuple[Union[BaseConfig, PlanningConfig, ChunkedConfig], SamplerConfig]:
        mcts_type = default_type or questionary.select(
            "Select MCTS type:",
            choices=['normal', 'chunked', 'planning', 'objective'],
            instruction="Choose MCTS algorithm variant. Planning is recommended for complex tasks."
        ).ask()

        #model = "ft:gpt-4o-mini-2024-07-18:paperplane-ai:mcts-full:AbYn5Pj6" #"ft:gpt-4o-mini-2024-07-18:paperplane-ai:mcts-pruned-masked:AYIViDdb"
        model = "ft:gpt-4o-mini-2024-07-18:paperplane-ai:mcts-pruned-masked:AYIViDdb"
        if mcts_type != 'planning':
            model = questionary.text(
                "Model name:",
                default=model,
                instruction="Model to use for program synthesis, e.g. ft:gpt-4o-mini-2024-07-18:paperplane-ai:mcts-pruned-masked:AYIViDdb"
            ).ask()

        base_config = {
            'mcts_type': MCTSType(mcts_type),
            'model': model,
            'version': int(questionary.text(
                "Version:",
                default=str(default_version),
                instruction="The run version number. Higher versions may include bug fixes or improvements."
            ).ask()),
            'n_parallel': int(questionary.text(
                "Number of parallel instances:",
                default="4"
            ).ask()),
            'presence_penalty': float(questionary.text(
                'Fixed presence penalty applied across previously sampled logits. -2 to 2.',
                default='0'
            ).ask()),
            'frequency_penalty': float(questionary.text(
                'Dynamic frequency penalty applied across previously sampled logits. -2 to 2.',
                default='0'
            ).ask()),
            'system_prompt': ''
        }


        if mcts_type == 'planning':
            mcts_config = PlanningConfig(
                **base_config,
                planning_model=questionary.text(
                    "Planning model:",
                    default="claude-3-5-sonnet-20241022",
                    instruction="The model that samples plans by reasoning over objectives and game states."
                ).ask(),
                executor_model=questionary.text(
                    "Executor model:",
                    default="ft:gpt-4o-2024-08-06:paperplane-ai:fact-instruct-1:ATSVGf4d:ckpt-step-214",
                    instruction="The model that samples programs."
                ).ask(),
                objective_model=questionary.text(
                    "Objective model:",
                    default="ft:gpt-4o-2024-08-06:paperplane-ai:fact-self-gen-planning:AQzcPI91",
                    instruction="The model that generates new objectives."
                ).ask(),
                max_steps_per_objective=int(questionary.text(
                    "Maximum steps per objective:",
                    default="12",
                ).ask()),
                number_of_steps_for_judge=int(questionary.text(
                    "Number of steps for judge:",
                    default="3",
                    instruction="The branching factor for the planning tree. Higher values increase quality but use more tokens."
                ).ask()),
                step_executor_prompt_path=Path("../../prompts/bottoms_up_prompts/finetuning_prompts/step_supervised"),
                step_generator_prompt_path=Path("../../prompts/bottoms_up_prompts/finetuning_prompts/step_generator"),
                step_judge_prompt_path=Path("../../prompts/bottoms_up_prompts/finetuning_prompts/step_judge"),
                example_plan_prompt_path=Path("../../prompts/bottoms_up_prompts/finetuning_prompts/executor_plan")
            )
        elif mcts_type == 'objective':
            mcts_config = ObjectiveConfig(**base_config, objective_model=questionary.text(
                    "Objective model:",
                    default="ft:gpt-4o-mini-2024-07-18:paperplane-ai:plans-tree:AcZ8gHSo",
                    instruction="The model that samples objectives."
                ).ask())
        elif mcts_type == 'chunked':
            mcts_config = ChunkedConfig(**base_config)
        else:
            mcts_config = BaseConfig(**base_config)

        sampler_config = None

        mcts_config.sampler_type = SamplerType(questionary.select(
            "Select MCTS node sampler type:",
            choices=['kld', 'weighted reward'],
            instruction="Choose the sampling method for selecting actions. KLD priorities varied game states. Weighted reward prioritizes high-reward states."
        ).ask())

        if mcts_config.sampler_type == SamplerType.KLD:
            sampler_config = SamplerConfig(
                temperature=float(questionary.text(
                    "Temperature:",
                    default="1",
                    instruction="Higher values are closer to uniform sampling. Zero means greedy sampling from reward."
                ).ask()),
                window_size=int(questionary.text(
                    "Window size:",
                    default="100",
                    instruction="The number of recent programs to consider when sampling the next node"
                ).ask())
            )
        elif mcts_config.sampler_type == SamplerType.WEIGHTED_REWARD:
            compression_strength = float(questionary.text(
                "Compression strength:",
                instruction="Between 0-1. Higher values mean more exploration. Lower means more exploitation. -1 means adaptively cycle",
                default="1").ask())
            if compression_strength < 0:
                compression_strength = None

            sampler_config = SamplerConfig(
                compression_strength=compression_strength,
                max_conversation_length=int(questionary.text(
                    "Maximum conversation length:",
                    instruction="The maximum number of steps in the dialogue",
                    default="20").ask()),
            )
            sampler_config.adaptive_period = int(questionary.text(
                "Adaptive period:",
                instruction="The period for cycling exploration and exploitation",
                default="200").ask()) if sampler_config.compression_strength != -1 else None

        version_description = ""
        for key, value in mcts_config.__dict__.items():
            if isinstance(value, Path):
                value = str(value)
            version_description += f"{key}:{value}\n"

        for key, value in sampler_config.__dict__.items():
            if isinstance(value, Path):
                value = str(value)
            version_description += f"{key}:{value}\n"


        mcts_config.version_description = version_description

        return mcts_config, sampler_config
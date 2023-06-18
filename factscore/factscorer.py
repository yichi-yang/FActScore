import argparse
import string
import json
import numpy as np
import os
import logging

from tqdm import tqdm
from factscore.atomic_facts import AtomicFactGenerator
from factscore.clm import CLM
from factscore.npm import NPM
from factscore.openai_lm import OpenAIModel
from factscore.retrieval import DocDB, Retrieval

class FactScorer(object):

    def __init__(self,
                 model_name="retrieval+ChatGPT",
                 data_dir=".cache/factscore",
                 model_dir=".cache/factscore",
                 cache_dir=".cache/factscore",
                 openai_key="api.key",
                 cost_estimate="consider_cache",
                 batch_size=256):
        assert model_name in ["retrieval+llama", "retrieval+llama+npm", "retrieval+ChatGPT", "npm", "retrieval+ChatGPT+npm"]
        self.model_name = model_name

        self.db = {}
        self.retrieval = {}
        self.npm = {}
        self.batch_size = batch_size # batch size for retrieval
        self.openai_key = openai_key

        self.data_dir = data_dir
        self.cache_dir = cache_dir
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        self.af_generator = None
        self.cost_estimate = cost_estimate

        if "llama" in model_name:
            self.lm = CLM("inst-llama-7B",
                          model_dir=os.path.join(model_dir, "inst-llama-7B"),
                          cache_file=os.path.join(cache_dir, "inst-llama-7B.pkl"))
        elif "ChatGPT" in model_name:
            self.lm = OpenAIModel("ChatGPT",
                                  cache_file=os.path.join(cache_dir, "ChatGPT.pkl"),
                                  key_path=openai_key)
        else:
            self.lm = None

    def save_cache(self):
        if self.lm:
            self.lm.save_cache()
        if "npm" in self.model_name:
            for k, v in self.npm.items():
                v.save_cache()
        for k, v in self.retrieval.items():
            v.save_cache()

    def register_knowledge_source(self, name="enwiki-20230401", db_path=None, data_path=None):
        assert name not in self.retrieval, f"{name} already registered"
        if db_path is None:
            db_path = os.path.join(self.data_dir, f"{name}.db")

        if data_path is None:
            data_path = os.path.join(self.data_dir, f"{name}.jsonl")

        cache_path = os.path.join(self.cache_dir, f"retrieval-{name}.json")
        embed_cache_path = os.path.join(self.cache_dir, f"retrieval-{name}.pkl")

        self.db[name] = DocDB(db_path=db_path, data_path=data_path)
        self.retrieval[name] = Retrieval(self.db[name], cache_path, embed_cache_path, batch_size=self.batch_size)
        if "npm" in self.model_name:
            cache_path = os.path.join(self.cache_dir, f"bm25-{name}.json")
            embed_cache_path = os.path.join(self.cache_dir, f"bm25-{name}.pkl")
            self.npm[name] = NPM(Retrieval(self.db[name], cache_path, embed_cache_path, "bm25"),
                                 "npm-single",
                                 cache_file=os.path.join(self.cache_dir, f"npm-{name}.pkl"))


    def print_cost_estimates(self, total_words, task, model):
        # https://help.openai.com/en/articles/4936856-what-are-tokens-and-how-to-count-them
        # Number of tokens are roughly 4/3 of the number of words
        total_tokens = total_words * 4.0 / 3

        # https://openai.com/pricing
        # if we use davinci-003, the cost is $0.02 per 1000 tokens
        # if we use gpt-3.5-turbo, the cost is $0.002 per 1000 tokens
        if model == "davinci-003":
            rate = 0.02
        elif model == "gpt-3.5-turbo":
            rate = 0.002

        total_cost = total_tokens * rate / 1000

        # print the total words, tokens, and cost along with rate
        logging.critical("Estimated OpenAI API cost for %s ($%.3f per 1000 tokens): $%.2f for %d words and %d tokens" % (task, rate, total_cost, total_words, total_tokens))

    def get_score(self,
                  generations,
                  topics=None,
                  atomic_facts=None,
                  knowledge_source=None,
                  verbose=False,
                  passages=None):

        if knowledge_source is None:
            # use the default one (enwiki-20230401)
            knowledge_source = "enwiki-20230401"
            if passages is None and knowledge_source not in self.retrieval:
                self.register_knowledge_source(knowledge_source)
        else:
            assert knowledge_source in self.retrieval, \
                f"{knowledge_source} is not registered yet. Please use `register_knowledge_source()` function to register it with a database"

        if type(generations) == str:
            generations = [generations]

        if type(topics) == str:
            topics = [topics]
        elif topics is None:
            assert passages is not None, "`passages` are required if `topics` are not provided"
            topics = [None] * len(generations)

        assert type(topics)==type(generations)==list, "`topics` and `generations` should be lists."
        assert len(topics)==len(generations), "`topics` and `generations` should have the same length"

        if passages is None:
            passages = [None] * len(generations)

        if atomic_facts is not None:
            assert len(topics)==len(atomic_facts), "`topics` and `atomic_facts` should have the same length"
        else:
            if self.af_generator is None:
                self.af_generator = AtomicFactGenerator(key_path=self.openai_key,
                                                        demon_dir=os.path.join(self.data_dir, "demos"),
                                                        gpt3_cache_file=os.path.join(self.cache_dir, "InstructGPT.pkl"))

            # estimate the total cost of atomic fact generation
            total_words = 0
            for gen in generations:
                total_words += self.af_generator.run(gen, cost_estimate=self.cost_estimate)

            self.print_cost_estimates(total_words, task="atomic fact generation", model="davinci-003")

            if verbose:
                topics = tqdm(topics)

            atomic_facts = []
            for gen in generations:
                curr_afs, _ = self.af_generator.run(gen)
                curr_afs = [fact for _, facts in curr_afs for fact in facts]
                if len(curr_afs)==0:
                    atomic_facts.append(None)
                else:
                    atomic_facts.append(curr_afs)
                if len(atomic_facts) % 10 == 0:
                    self.af_generator.save_cache()

            assert len(atomic_facts)==len(topics)
            self.af_generator.save_cache()

        respond_ratio = np.mean([facts is not None for facts in atomic_facts])

        if "ChatGPT" in self.model_name:
            # estimate the total cost of response generation
            total_words = 0
            for topic, generation, facts, psgs in zip(topics, generations, atomic_facts, passages):
                if facts is not None:
                    total_words += self._get_score(topic, generation, facts, knowledge_source, cost_estimate=self.cost_estimate, passages=psgs)

            self.print_cost_estimates(total_words, task="factscore evaluation", model="gpt-3.5-turbo")

        if verbose:
            topics = tqdm(topics)

        scores = []
        raw_scores = []
        decisions = []
        for topic, generation, facts, psgs in zip(topics, generations, atomic_facts, passages):
            if facts is None:
                decisions.append(None)
                raw_scores.append(None)
            else:
                decision = self._get_score(topic, generation, facts, knowledge_source, passages=psgs)
                score = np.mean([d["is_supported"] for d in decision])
                decisions.append(decision)
                scores.append(score)
                raw_scores.append(score)
                if len(scores) % 10 == 0:
                    self.save_cache()

        self.save_cache()

        return {"score": np.mean(scores),
                "raw_scores": raw_scores,
                "respond_ratio": respond_ratio,
                "decisions": decisions,
                "num_facts_per_response": np.mean([len(d) for d in decisions if d is not None])}

    def _get_score(self, topic, generation, atomic_facts, knowledge_source, cost_estimate=None, passages=None):
        decisions = []
        total_words = 0
        for atom in atomic_facts:
            atom = atom.strip()
            if self.lm:
                if passages is None:
                    passages = self.retrieval[knowledge_source].get_passages(topic, atom, k=5)
                if topic is not None:
                    definition = "Answer the question about {} based on the given context.\n\n".format(topic)
                else:
                    definition = "Answer the question based on the given context.\n\n"
                context = ""
                for psg_idx, psg in enumerate(reversed(passages)):
                    title = psg.get("title")
                    text = psg["text"].replace("<s>", "").replace("</s>", "")
                    if title is not None:
                        context += "Title: {}\nText: {}\n\n".format(title, text)
                    else:
                        context += "Text: {}\n\n".format(text)

                definition += context.strip()
                if not definition[-1] in string.punctuation:
                    definition += "."
                prompt = "{}\n\nInput: {} True or False?\nOutput:".format(definition.strip(), atom.strip())

                if cost_estimate:
                    if cost_estimate == "consider_cache" and (prompt.strip() + "_0") not in self.lm.cache_dict:
                        total_words += len(prompt.split())
                    elif cost_estimate == "ignore_cache":
                        total_words += len(prompt.split())
                    continue

                output = self.lm.generate(prompt)

                if type(output[1])==np.ndarray:
                    # when logits are available
                    logits = np.array(output[1])
                    assert logits.shape[0] in [32000, 32001]
                    true_score = logits[5852]
                    false_score = logits[7700]
                    is_supported = true_score > false_score
                else:
                    # when logits are unavailable
                    generated_answer = output[0].lower()
                    if "true" in generated_answer or "false" in generated_answer:
                        if "true" in generated_answer and "false" not in generated_answer:
                            is_supported = True
                        elif "false" in generated_answer and "true" not in generated_answer:
                            is_supported = False
                        else:
                            is_supported = generated_answer.index("true") > generated_answer.index("false")
                    else:
                        is_supported = all([keyword not in generated_answer.lower().translate(str.maketrans("", "", string.punctuation)).split() for keyword in ["not", "cannot", "unknown", "information"]])

            else:
                is_supported = True

            if is_supported and "npm" in self.model_name:
                npprob = self.npm[knowledge_source].get_probabilty(topic, atom)
                is_supported = npprob > 0.3

            decisions.append({"atom": atom, "is_supported": is_supported})

        if cost_estimate:
            return total_words
        else:
            return decisions

if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--input_path',
                        type=str,
                        default="data/labeled/InstructGPT.jsonl")
    parser.add_argument('--model_name',
                        type=str,
                        default="retrieval+ChatGPT")
    parser.add_argument('--openai_key',
                        type=str,
                        default="api.key")
    parser.add_argument('--data_dir',
                        type=str,
                        default=".cache/factscore/")
    parser.add_argument('--model_dir',
                        type=str,
                        default=".cache/factscore/")
    parser.add_argument('--cache_dir',
                        type=str,
                        default=".cache/factscore/")
    parser.add_argument('--cost_estimate',
                        type=str,
                        default="consider_cache",
                        choices=["consider_cache", "ignore_cache"])
    parser.add_argument('--use_atomic_facts',
                        action="store_true")
    parser.add_argument('--ignore_topics',
                        action="store_true")
    parser.add_argument('--use_passages',
                        action="store_true")
    parser.add_argument('--verbose',
                        action="store_true",
                        help="for printing out the progress bar")
    parser.add_argument('--print_rate_limit_error',
                        action="store_true",
                        help="for printing out rate limit error when using OpenAI keys")
    parser.add_argument('--n_samples',
                        type=int,
                        default=None)
    parser.add_argument('--result_save_path',
                        type=str,
                        default=None)

    args = parser.parse_args()

    logging.basicConfig(format='%(asctime)s - %(name)s - %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.ERROR if args.print_rate_limit_error else logging.CRITICAL)

    fs = FactScorer(model_name=args.model_name,
                    data_dir=args.data_dir,
                    model_dir=args.model_dir,
                    cache_dir=args.cache_dir,
                    openai_key=args.openai_key,
                    cost_estimate=args.cost_estimate)

    tot = 0
    topics, generations, atomic_facts, passages = [], [], [], []
    with open(args.input_path) as f:
        for line in f:
            dp = json.loads(line)
            tot += 1

            generations.append(dp["output"])

            if args.use_atomic_facts:
                assert "annotations" in dp, "You can specify `--use_atomic_facts` only when atomic facts are available in the input data already."
                if dp["annotations"] is None:
                    continue
                atomic_facts.append([atom["text"] for sent in dp["annotations"] for atom in sent["model-atomic-facts"]])

            if not args.ignore_topics:
                topics.append(dp["topic"])

            if args.use_passages:
                passages.append(dp["passages"])

            if args.n_samples is not None and tot==args.n_samples:
                break
    out = fs.get_score(topics=topics if not args.ignore_topics else None,
                       generations=generations,
                       atomic_facts=atomic_facts if args.use_atomic_facts else None,
                       passages=passages if args.use_passages else None,
                       verbose=args.verbose)
    logging.critical("FActScore = %.1f%%" % (100*out["score"]))
    logging.critical("Respond ratio = %.1f%%" % (100*out["respond_ratio"]))
    logging.critical("# Atomic facts per valid response = %.1f" % (out["num_facts_per_response"]))

    if args.result_save_path:
        with open(args.result_save_path, 'w', encoding='utf8') as output:
            json.dump(out, output)




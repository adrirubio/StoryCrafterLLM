import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import tiktoken
from datasets import load_dataset
import matplotlib.pyplot as plt
import numpy as np
from datetime import datetime
import os

# Define hyperparameters
vocab_size = 50257
n_heads = 8
n_layers = 6
head_size = 64
n_embd = 512
block_size = 128
dropout = 0.1
learning_rate = 3e-4
weight_decay = 0.1

# Set Hugging Face cache directories on the external disk
os.environ['HF_HOME'] = '/media/adrian/FamilyBackup/adrian_ai_workspace/hf_cache'
os.environ['HF_DATASETS_CACHE'] = '/media/adrian/FamilyBackup/adrian_ai_workspace/datasets_cache'

# Load the BookCorpus dataset and ensure it's cached on the external disk
dataset = load_dataset("bookcorpus", cache_dir='/media/adrian/FamilyBackup/adrian_ai_workspace/')

# Keep only 10% of the dataset
total_samples = len(dataset["train"])
one_percent_samples = int(total_samples * 0.001)
dataset_subset = dataset["train"].select(range(one_percent_samples))  # Select only the first 1%

# Split the subset into train (90%) and test (10%)
split_dataset = dataset_subset.train_test_split(test_size=0.1)  # 10% for testing
train_dataset = split_dataset["train"]
test_dataset = split_dataset["test"]

# Print the size of the train and the test sets
print(f"Train size: {len(train_dataset)}")
print(f"Test size: {len(test_dataset)}")

# Initialize the tiktoken encoder
enc = tiktoken.get_encoding("gpt2")

# Define the tokenization function
def tokenize_function(examples):
    return {
        "input_ids": [enc.encode(text) for text in examples["text"]],
        "attention_mask": [[1] * len(enc.encode(text)) for text in examples["text"]]
    }

# Function to pad or truncate sequences
def pad_or_truncate(batch):
    max_length = 512
    for key in ['input_ids', 'attention_mask']:
        batch[key] = [
            seq[:max_length] + [0] * (max_length - len(seq)) if len(seq) < max_length else seq[:max_length]
            for seq in batch[key]
        ]
    return batch

# Tokenize and process the datasets
def process_dataset(dataset, split_name):
    # Tokenize
    tokenized_dataset = dataset.map(
        tokenize_function,
        batched=True,
        num_proc=20,
        remove_columns=dataset.column_names
    )

    # Pad or truncate
    processed_dataset = tokenized_dataset.map(
        pad_or_truncate,
        batched=True,
        num_proc=20,
    )

    # Set format to PyTorch tensors
    processed_dataset.set_format(type="torch", columns=["input_ids", "attention_mask"])

    return processed_dataset

# Process both train and test datasets
train_dataset = process_dataset(train_dataset, "train")
test_dataset = process_dataset(test_dataset, "test")

# Print some examples
print(f"Example train data: {train_dataset[0]}")
print(f"Example test data: {test_dataset[0]}")

# Create DataLoaders
train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=8, shuffle=True)
test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=8, shuffle=False)

# Print an example batch
for batch in train_loader:
    print(f"Batch input ids shape: {batch['input_ids'].shape}")
    print(f"Batch attention mask shape: {batch['attention_mask'].shape}")
    break

# Print an example batch
for batch in train_loader:
    print(f"Batch input ids shape: {batch['input_ids'].shape}")
    print(f"Batch attention mask shape: {batch['attention_mask'].shape}")
    break

# Define model
class Head(nn.Module):
    """ One head of self-attention """
    def __init__(self, head_size, n_embd, block_size, dropout):
        super().__init__()
        self.key = nn.Linear(n_embd, head_size, bias=False)
        self.query = nn.Linear(n_embd, head_size, bias=False)
        self.value = nn.Linear(n_embd, head_size, bias=False)
        self.register_buffer("tril", torch.tril(torch.ones(block_size, block_size)))

        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        B, T, C = x.shape
        k = self.key(x)
        q = self.query(x)
        v = self.value(x)

        assert C == self.key.in_features, f"Input size {C} doesn't match expected size {self.key.in_features}"

        wei = q @ k.transpose(-2, -1) * k.shape[-1]**-0.5
        wei = wei.masked_fill(self.tril[:T, :T] == 0, float('-inf'))
        wei = F.softmax(wei, dim=-1)
        wei = self.dropout(wei)

        out = wei @ v
        return out

class MultiHeadAttention(nn.Module):
    """ Multiple heads of self-attention in parallel """

    def __init__(self, n_heads, head_size, n_embd, dropout):
        super().__init__()
        self.heads = nn.ModuleList([Head(head_size, n_embd, block_size, dropout) for _ in range(n_heads)])
        self.proj = nn.Linear(n_heads *  head_size, n_embd)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        # Collects the outputs from each head
        head_outputs = [head(x) for head in self.heads]
        # Concatenate the outputs
        concatenated = torch.cat(head_outputs, dim=-1)
        # Apply linear transformation and dropout
        out = self.proj(concatenated)
        out = self.dropout(out)
        return out


class FeedForward(nn.Module):
    """ A simple linear layer followed by non-linearity """

    def __init__(self, n_embd, dropout=0.1, expansion_factor=4):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_embd, expansion_factor * n_embd),
            nn.ReLU(),
            nn.Linear(expansion_factor * n_embd, n_embd),
            nn.Dropout(dropout),
        )

    def forward(self, x):
        return self.net(x)

class Block(nn.Module):
    """ Transformer block: communication followed by computation """

    def __init__(self, n_embd, n_head, dropout=0.1):
        # n_embed: embedding dimension, n_head: the number of heads we'd like
        super().__init__()
        head_size = n_embd // n_head
        self.sa = MultiHeadAttention(n_head, head_size, n_embd, dropout)
        self.ffwd = FeedForward(n_embd, dropout)
        self.ln1 = nn.LayerNorm(n_embd)
        self.ln2 = nn.LayerNorm(n_embd)

    def forward(self, x):
        x = x + self.sa(self.ln1(x))
        x = x + self.ffwd(self.ln2(x))
        return x

class GPTLanguageModel(nn.Module):
    def __init__(self, vocab_size, n_embd, block_size, n_layer, n_head, device="cpu"):
        super().__init__()
        self.device = device
        self.block_size = block_size
        self.token_embedding_table = nn.Embedding(vocab_size, n_embd)
        self.position_embedding_table = nn.Embedding(block_size, n_embd)
        self.blocks = nn.Sequential(*[Block(n_embd, n_head) for _ in range(n_layer)])
        self.ln_f = nn.LayerNorm(n_embd)
        self.lm_head = nn.Linear(n_embd, vocab_size)
        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, mean=0.1, std=0.02)

    def forward(self, idx, targets=None):
        B, T = idx.shape

        # Truncate sequence length to block_size
        T = min(T, self.block_size)
        idx = idx[:, :T]

        # Get token embeddings for input indices
        tok_emb = self.token_embedding_table(idx)  # (B, T, C)

        # Get position embeddings (truncate to match input length)
        pos_emb = self.position_embedding_table(torch.arange(T, device=idx.device))  # (T, C)

        # Combine token and position embeddings
        x = tok_emb + pos_emb.unsqueeze(0)  # (B, T, C)

        # Apply transformer blocks
        x = self.blocks(x)  # (B, T, C)

        # Final layer normalization
        x = self.ln_f(x)  # (B, T, C)

        # Get logits for vocabulary prediction
        logits = self.lm_head(x)  # (B, T, vocab_size)

        # Optionally calculate loss if targets are provided
        loss = None
        if targets is not None:
            # Ensure targets are the same size as logits
            targets = targets[:, :T]
            B, T, C = logits.shape
            logits = logits.reshape(B*T, C)
            targets = targets.reshape(B*T)
            loss = F.cross_entropy(logits, targets)

        return logits, loss

    @torch.no_grad()
    def generate(self, idx, max_new_tokens):
        for _ in range(max_new_tokens):
            idx_cond = idx[:, -self.block_size:]  # Crop to the last block_size tokens
            logits, _ = self(idx_cond)  # Get Predictions
            logits = logits[:, -1, :]  # Focus on the last time step
            probs = F.softmax(logits, dim=-1)  # Get probabilities
            idx_next = torch.multinomial(probs, num_samples=1)  # Samples from the distribution
            idx = torch.cat((idx, idx_next), dim=1)  # Append sampled index
        return idx

device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
print (f"Using device: {device}")

# Instantiate the model
model = GPTLanguageModel(vocab_size, n_embd, block_size, n_layers, n_heads, device=device)

# Move the model to the GPU (if available)
model = model.to(device)

# Define criterion and optimizer
criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=learning_rate)

# Training loop with progress reporting
def batch_gh(model, criterion, optimizer, train_loader, test_loader, epochs):
    train_losses = np.zeros(epochs)
    test_losses = np.zeros(epochs)

    for it in range(epochs):
        model.train()  # Set model to training mode
        t0 = datetime.now()
        train_loss = []

        for i, batch in enumerate(train_loader):
            inputs = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)

            # Create targets by shifting inputs by one position
            targets = inputs[:, 1:].contiguous()
            inputs = inputs[:, :-1].contiguous()

            # Zero parameter gradients
            optimizer.zero_grad()

            # Forward pass
            outputs, loss = model(inputs, targets)

            # Backward and optimize
            loss.backward()
            optimizer.step()

            train_loss.append(loss.item())

            # Print progress every 100 batches
            if (i + 1) % 100 == 0:
                print(f'Epoch {it + 1}/{epochs}, Batch {i + 1}/{len(train_loader)}, Loss: {loss.item():.4f}')

        # Get average train_loss
        train_loss = np.mean(train_loss)

        model.eval()  # Set model to evaluation mode
        test_loss = []
        with torch.no_grad():
            for batch in test_loader:
                inputs = batch["input_ids"].to(device)
                attention_mask = batch["attention_mask"].to(device)

                # Create targets by shifting inputs by one position
                targets = inputs[:, 1:].contiguous()
                inputs = inputs[:, :-1].contiguous()

                outputs, loss = model(inputs, targets)
                test_loss.append(loss.item())

            test_loss = np.mean(test_loss)

        # Save losses
        train_losses[it] = train_loss
        test_losses[it] = test_loss

        dt = datetime.now() - t0
        print(f'Epoch {it + 1}/{epochs}, Train Loss: {train_loss:.4f}, '
              f'Test Loss: {test_loss:.4f}, Duration: {dt}')

    return train_losses, test_losses

# Run the training
train_losses, test_losses = batch_gh(model, criterion, optimizer, train_loader, test_loader, epochs=2)

# Plot loss
plt.plot(train_losses, label="train_loss")
plt.plot(test_losses, label="test_loss")
plt.legend()
plt.show()

# Save model weights
model_save_path = "/home/adrian/Documents/StoryCrafterLLM/model_weights.pth"
torch.save(model.state_dict(), model_save_path)
print(f"Model saved to {model_save_path}")

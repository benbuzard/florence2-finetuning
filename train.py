import os

import torch
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import (AdamW, AutoModelForCausalLM, AutoProcessor,
                          get_scheduler)
from datasets import Dataset, Image, Sequence

from data import (GrabberDataset, generate_grabber_data)
import PIL

# Set device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Load the model and processor
model = AutoModelForCausalLM.from_pretrained(
    "microsoft/Florence-2-large-ft", trust_remote_code=True
).to(device)
processor = AutoProcessor.from_pretrained(
    "microsoft/Florence-2-large-ft", trust_remote_code=True
)


def collate_fn(batch):
    questions, answers, images = zip(*batch)
    inputs = processor(
        text=list(questions), images=list(images), return_tensors="pt", padding=True
    ).to(device)
    return inputs, answers

def data_split(data):
    split_index = len(data) - len(data)//10
    return data[:split_index], data[split_index:]

test_image = PIL.Image.open("test.jpg")
def count_tokens(text):
    return len(processor(text=text, images=[test_image], return_tensors="np", padding=True)['input_ids'][0])

prompts, answers, images = generate_grabber_data("Grabber", count_tokens)

train_prompts, val_prompts = data_split(prompts)
train_answers, val_answers = data_split(answers)
train_images, val_images = data_split(images)

# Create datasets
train_dataset = Dataset.from_dict({'prompt': train_prompts, 'answer': train_answers, 'image': train_images})
train_dataset.cast_column('image', Image())
val_dataset = Dataset.from_dict({'prompt': val_prompts, 'answer': val_answers, 'image': val_images})
val_dataset.cast_column('image', Image())

train_dataset = GrabberDataset(train_dataset)
val_dataset = GrabberDataset(val_dataset)

# Create DataLoader
batch_size = 1
num_workers = 0

train_loader = DataLoader(
    train_dataset,
    batch_size=batch_size,
    collate_fn=collate_fn,
    num_workers=num_workers,
    shuffle=True,
)
val_loader = DataLoader(
    val_dataset, batch_size=batch_size, collate_fn=collate_fn, num_workers=num_workers
)


def train_model(train_loader, val_loader, model, processor, epochs=10, lr=1e-6):
    optimizer = AdamW(model.parameters(), lr=lr)
    num_training_steps = epochs * len(train_loader)
    lr_scheduler = get_scheduler(
        name="linear",
        optimizer=optimizer,
        num_warmup_steps=0,
        num_training_steps=num_training_steps,
    )

    for epoch in range(epochs):
        # Training phase
        model.train()
        train_loss = 0
        i = -1
        for batch in tqdm(train_loader, desc=f"Training Epoch {epoch + 1}/{epochs}"):
            i += 1
            inputs, answers = batch

            input_ids = inputs["input_ids"]
            pixel_values = inputs["pixel_values"]
            labels = processor.tokenizer(
                text=answers,
                return_tensors="pt",
                padding=True,
                return_token_type_ids=False,
            ).input_ids.to(device)

            outputs = model(
                input_ids=input_ids, pixel_values=pixel_values, labels=labels
            )
            loss = outputs.loss

            if i % 200 == 0:
                print(loss)

                generated_ids = model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=1024,
                    num_beams=3,
                )
                generated_texts = processor.batch_decode(
                    generated_ids, skip_special_tokens=False
                )

                for generated_text, answer in zip(generated_texts, answers):
                    parsed_answer = processor.post_process_generation(
                        generated_text,
                        task="<MORE_DETAILED_DANBOORU_CAPTION>",
                        image_size=(
                            inputs["pixel_values"].shape[-2],
                            inputs["pixel_values"].shape[-1],
                        ),
                    )
                    print("GT:", answer)
                    print("Pred:", parsed_answer["<MORE_DETAILED_DANBOORU_CAPTION>"])

            loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()

            train_loss += loss.item()

        avg_train_loss = train_loss / len(train_loader)
        print(f"Average Training Loss: {avg_train_loss}")

        # Validation phase
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for batch in tqdm(
                val_loader, desc=f"Validation Epoch {epoch + 1}/{epochs}"
            ):
                inputs, answers = batch

                input_ids = inputs["input_ids"]
                pixel_values = inputs["pixel_values"]
                labels = processor.tokenizer(
                    text=answers,
                    return_tensors="pt",
                    padding=True,
                    return_token_type_ids=False,
                ).input_ids.to(device)

                outputs = model(
                    input_ids=input_ids, pixel_values=pixel_values, labels=labels
                )
                loss = outputs.loss

                val_loss += loss.item()

        avg_val_loss = val_loss / len(val_loader)
        print(f"Average Validation Loss: {avg_val_loss}")

        # Save model checkpoint
        output_dir = f"./model_checkpoints/epoch_{epoch+1}"
        os.makedirs(output_dir, exist_ok=True)
        model.save_pretrained(output_dir)
        processor.save_pretrained(output_dir)

train_model(train_loader, val_loader, model, processor, epochs=3)

from argparse import ArgumentParser
from dataloader import DataLoader
from autoencoder import Autoencoder
import os
import glob
import shutil
from time import time
import numpy as np
from datetime import datetime
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
from tensorflow.keras.mixed_precision import experimental as mixed_precision

os.environ['CUDA_VISIBLE_DEVICES'] = "1"
os.environ['TF_CPP_MIN_LOG_LEVEL'] = "2"
policy = mixed_precision.Policy('float32')

tf.config.set_soft_device_placement(True)
# tf.debugging.set_log_device_placement(True)
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
  try:
    # Currently, memory growth needs to be the same across GPUs
    for gpu in gpus:
      tf.config.experimental.set_memory_growth(gpu, True)
    logical_gpus = tf.config.experimental.list_logical_devices('GPU')
    print(len(gpus), "Physical GPUs,", len(logical_gpus), "Logical GPUs")
  except RuntimeError as e:
    # Memory growth must be set before GPUs have been initialized
    print(e)

def get_path(path):
  return os.path.realpath(os.path.expanduser(os.path.expandvars(path)))

def renorm(image):
  return tf.clip_by_value((image + 1)/2, 0, 1)

def autoscale(image, scale=1):
  return scale * (image - np.min(image))/np.ptp(image)

def tf2image(image, norm=True):
  if norm:
    image = renorm(image)
  else:
    image = autoscale(image)
  return tf.cast(255 * image, tf.uint8)

def sobel_variation(image):
  sobel = tf.image.sobel_edges(renorm(image))
  dx = sobel[..., 0] / 4
  dy = sobel[..., 1] / 4
  g = tf.sqrt(tf.square(dx) + tf.square(dy))
  return g

def high_pass_x_y(image):
  x_var = image[:,:,1:,:] - image[:,:,:-1,:]
  y_var = image[:,1:,:,:] - image[:,:-1,:,:]
  return x_var[:,:-1,:,:], y_var[:,:,:-1,:]

def total_variation(image):
  dx, dy = high_pass_x_y(image)
  total_var = tf.abs(dx) + tf.abs(dy)
  return total_var

@tf.function
def train_step(model, x, y):
  """
  Single step of generator pre-training.
  Args:
    model: A model object with a tf keras compiled generator.
    x: The low resolution image tensor.
    y: The high resolution image tensor.
  """
  # Label smoothing for better gradient flow
  # valid = tf.ones((x.shape[0],) + model.disc_patch)
  # fake = tf.zeros((x.shape[0],) + model.disc_patch)

  loss_object = keras.losses.BinaryCrossentropy()

  with tf.GradientTape() as gen_tape, tf.GradientTape() as disc_tape:
    # Generate denoised image
    fake_hr = model.generator(x, training=True)

    # Train discriminators (original images = real ; generated = Fake)
    valid_prediction = model.discriminator(y, training=True)
    fake_prediction = model.discriminator(fake_hr, training=True)

    # Generator Loss
    content_loss = model.content_loss(y, fake_hr)
    # adv_loss = 1e-3 * tf.keras.losses.BinaryCrossentropy()(tf.ones_like(fake_prediction), fake_prediction)
    adv_loss = 1e-3 * loss_object(tf.ones_like(fake_prediction), fake_prediction)
    mse_loss = tf.keras.losses.MeanSquaredError()(y, fake_hr)
    mae_loss = tf.reduce_mean(tf.abs(y - fake_hr))
    perceptual_loss = content_loss + adv_loss + 0*mse_loss + mae_loss

    # Discriminator Loss
    # valid_loss = tf.keras.losses.BinaryCrossentropy()(tf.ones_like(valid_prediction), valid_prediction)
    # fake_loss = tf.keras.losses.BinaryCrossentropy()(tf.zeros_like(fake_prediction), fake_prediction)
    valid_loss = loss_object(tf.ones_like(valid_prediction), valid_prediction)
    fake_loss = loss_object(tf.zeros_like(fake_prediction), fake_prediction)
    disc_loss = valid_loss + fake_loss

  # Generator and Discriminator Gradients
  gen_grads = gen_tape.gradient(perceptual_loss, model.generator.trainable_variables)
  disc_grads = disc_tape.gradient(disc_loss, model.discriminator.trainable_variables)

  # Generator and Discriminator Backprop
  model.gen_optimizer.apply_gradients(zip(gen_grads, model.generator.trainable_variables))
  model.disc_optimizer.apply_gradients(zip(disc_grads, model.discriminator.trainable_variables))

  return disc_loss, adv_loss, content_loss, mse_loss, mae_loss

def train(model, dataset, args, writer):
  """
  Function that defines a single training step for the SR-GAN.
  Args:
    model: An object that contains tf keras compiled generator and
         discriminator models.
    dataset: A tf data object that contains low and high res images.
    log_iter: Number of iterations after which to add logs in
          tensorboard.
    writer: Summary writer
  """
  log_iter = args.save_iter

  with writer.as_default():
    # Iterate over dataset
    for img_input, img_target in dataset:
      disc_loss, adv_loss, content_loss, mse_loss, mae_loss = train_step(model, img_input, img_target)
      model.iterations += 1
      # Log tensorboard summaries if log iteration is reached.
      if model.iterations % log_iter == 0:
        # Losses
        tf.summary.scalar('Generator Losses/adv_loss', adv_loss, step=model.iterations)
        tf.summary.scalar('Generator Losses/content_loss', content_loss, step=model.iterations)
        tf.summary.scalar('Generator Losses/mse_loss', mse_loss, step=model.iterations)
        tf.summary.scalar('Generator Losses/mae_loss', mae_loss, step=model.iterations)
        tf.summary.scalar('Discriminator Losses/disc_loss', disc_loss, step=model.iterations)

        # Images
        img_gen = model.generator(img_input, training=False)
        dx_gen, dy_gen = high_pass_x_y(img_gen)
        dx_target, dy_target = high_pass_x_y(img_target)
        var_gen = total_variation(img_gen)
        var_target = total_variation(img_target)
        # print(f"Image Shapes: input: {img_input.shape}, target: {img_target.shape}, gen: {img_gen.shape}")
        tf.summary.image('Images/Input', tf2image(img_input), step=model.iterations)
        tf.summary.image('Images/Target', tf2image(img_target), step=model.iterations)
        tf.summary.image('Images/Generated', tf2image(img_gen), step=model.iterations)
        tf.summary.image('Error/Square Error (MSE)', tf2image(tf.square(img_gen-img_target), norm=False), step=model.iterations)
        tf.summary.image('Error/Absolute Error (MAE)', tf2image(tf.abs(img_gen-img_target), norm=False), step=model.iterations)
        tf.summary.image('Error/Sobel Variation', tf2image(sobel_variation(img_gen-img_target), norm=False), step=model.iterations)
        tf.summary.image('Error/Total Variation', tf2image(total_variation(img_gen-img_target), norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/Sobel Input', tf2image(sobel_variation(img_input), norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/Sobel Target', tf2image(sobel_variation(img_target), norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/Sobel Generated', tf2image(sobel_variation(img_gen), norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/dx Target', tf2image(dx_target, norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/dy Target', tf2image(dy_target, norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/dx Generated', tf2image(dx_gen, norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/dy Generated', tf2image(dy_gen, norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/Total Var Target', tf2image(var_target, norm=False), step=model.iterations)
        tf.summary.image('Image Gradients/Total Var Generated', tf2image(var_gen, norm=False), step=model.iterations)

        writer.flush()

  return disc_loss, adv_loss, content_loss, mse_loss, mae_loss

def main(args):
  # Collect Timestamp for training
  timestamp = datetime.now()
  time_long = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
  time_short = timestamp.strftime("%m%d_%H%M")
  date = timestamp.strftime("%m%d")

  # create directory for saving trained models.
  os.makedirs('models/checkpoints', exist_ok=True)
  os.makedirs('models/backups', exist_ok=True)
  os.makedirs('logs', exist_ok=True)

  # Calculate steps per epoch
  image_dir = get_path(args.image_dir)
  num_images = len(os.listdir(image_dir))
  batch_size = args.batch_size
  steps_per_epoch = num_images // batch_size
  print(f"Steps per epoch: {steps_per_epoch}")
  if args.save_iter > steps_per_epoch:
    args.save_iter = steps_per_epoch

  # Create the tensorflow dataset.
  ds = DataLoader(args).dataset()

  # Define the directory for saving the SRGAN training tensorbaord summary.
  logdir = get_path(args.logdir)
  # traindirs = glob.glob(os.path.join(logdir, f"train_{date}*"))
  # if traindirs:
  #   train_num = max([int(x.split('_')[-1]) for x in traindirs])
  #   train_num += 1
  # else:
  #   train_num = 1
  # traindir = os.path.join(logdir, f"train_{date}_{train_num}")
  traindir = os.path.join(logdir, f"train_{time_short}")
  train_summary_writer = tf.summary.create_file_writer(traindir)

  # Create Autoencoder model
  model = Autoencoder(args)

  # Create Checkpoint
  checkpoint_dir = get_path('models/checkpoints/autoencoder')
  os.makedirs(checkpoint_dir, exist_ok=True)
  # checkpoint_prefix = os.path.join(checkpoint_dir, "ckpt_ae")
  checkpoint = tf.train.Checkpoint(gen_optimizer=model.gen_optimizer,
                                   disc_optimizer=model.disc_optimizer,
                                   generator=model.generator,
                                   discriminator=model.discriminator)
  ckpt_manager = tf.train.CheckpointManager(checkpoint, checkpoint_dir, max_to_keep=3)
  if bool(args.retrain) == True:
    checkpoint.restore(ckpt_manager.latest_checkpoint).expect_partial()

  # Run training.
  for epoch in range(args.epochs):
    # print("====== Beginning epoch: {} ======".format(epoch))
    train_begin = time()
    disc_loss, adv_loss, content_loss, mse_loss, mae_loss = train(model, ds, args, train_summary_writer)
    train_end = time()
    train_time = train_end - train_begin
    if args.ckpt:
      if epoch % 5 == 0:
        ckpt_manager.save()
    end = time()
    save_time = end - train_end
    total_time = end - train_begin
    model.epochs += 1
    print(f"|== Finished epoch: {model.epochs}, disc_loss: {disc_loss:.2e}, adv_loss: {adv_loss:.2e}, vgg: {content_loss:.2e}, mse: {mse_loss:.2e}, mae: {mae_loss:.2e}, iters: {model.iterations}, train: {train_time:0.2f}, total: {total_time:0.2f} ==|")

  # Save final models
  if args.save_model:
    model.generator.save("models/autoencoder.h5")
    model.discriminator.save("models/discriminator_ae.h5")
    model.generator.save(f"models/backups/autoencoder_{time_short}.h5")
    model.discriminator.save(f"models/backups/discriminator_ae_{time_short}.h5")

if __name__ == '__main__':
  params = dict(
    model_name = "autoencoder",
    image_dir = get_path("train/image_input/DIV2K_train_HR"),
    model_dir = get_path("./models"),
    logdir = get_path("./logs"),
    batch_size = 1,
    epochs = 1,
    crop_size = 256,
    lr = 1e-3,
    save_iter = 200,
    retrain = 1,
    save_model = 1,
    ckpt = 1,
    fp16 = 0,
    scale = 1,
    jpeg_quality = 50
  )

  parser = ArgumentParser()

  for key, value in params.items():
    flag = str("--" + key)
    parser.add_argument(flag, default=value, type=type(value))

  # parser.add_argument('--image_dir', default=get_path(image_dir),type=str, help='Path to high resolution image directory.')
  # parser.add_argument('--batch_size', default=batch_size, type=int, help='Batch size for training.')
  # parser.add_argument('--epochs', default=epochs, type=int, help='Number of epochs for training')
  # parser.add_argument('--crop_size', default=crop_size, type=int, help='Low resolution input size.')
  # parser.add_argument('--lr', default=1e-3, type=float, help='Learning rate for optimizers.')
  # parser.add_argument('--save_iter', default=200, type=int, help='The number of iterations to save the tensorboard summaries and models.')
  # parser.add_argument('--model_dir', default="./models", type=str, help='Model directory if different from ./models/autoencoder.h5.')
  # parser.add_argument('--logdir', default="./logs", type=str, help='Tensorboard logdir.')
  # parser.add_argument('--retrain_model', default=False, type=bool, help='True for retraining current model in ./models directory.')
  # parser.add_argument('--save_model', default=True, type=bool, help='Save model during iterations.')

  args = parser.parse_args()
  args.retrain = bool(args.retrain)
  args.save_model = bool(args.save_model)
  args.ckpt = bool(args.ckpt)
  args.fp16 = bool(args.fp16)
  
  if args.fp16:
    policy = mixed_precision.Policy('mixed_float16')
  else:
    policy = mixed_precision.Policy('float32')

  mixed_precision.set_policy(policy)

  print("COMPUTATION PARAMETERS")
  print('Compute dtype: %s' % policy.compute_dtype)
  print('Variable dtype: %s' % policy.variable_dtype)

  for k, v in vars(args).items():
    print(f"  {k}:".ljust(15) + f"{repr(v)}".ljust(50) + f"['{type(v).__name__}']")

  main(args)

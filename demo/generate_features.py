import os
import sys
import logging


def patch_path(path):
    return os.path.join(os.path.dirname(__file__), path)


def extract_image_features():
    from mxnet_text_to_image.data.flowers_images import get_image_features
    feats = get_image_features(data_dir_path=patch_path('data/flowers/jpg'))
    logging.info('total %d images from which features are extracted', len(feats))


def extract_transformed_images():
    from mxnet_text_to_image.data.flowers_images import get_transformed_images
    feats = get_transformed_images(data_dir_path=patch_path('data/flowers/jpg'))
    logging.info('total %d images from which features are extracted', len(feats))


def extract_text_features():
    from mxnet_text_to_image.data.flowers_texts import get_text_features
    feats = get_text_features(data_dir_path=patch_path('data/flowers/text_c10'), glove_dir_path=patch_path('data/glove'))
    logging.info('total %d text features extracted', len(feats))


def main():
    sys.path.append(patch_path('..'))

    logging.basicConfig(level=logging.DEBUG)

    extract_image_features()
    extract_transformed_images()
    extract_text_features()


if __name__ == '__main__':
    main()

